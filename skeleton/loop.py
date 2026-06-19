"""self-improvement ループ（walking skeleton）。

1 サイクル = builder が候補 slate（複数提案）を生成 -> 各候補を search workload で
            sandbox(隔離適用+実テスト+実ベンチ) -> Reviewer 2体 -> Judge(決定的集約)
            -> gate.evaluate_gates(Bonferroni 補正・n_comparisons=slate サイズ) で採否
            -> valid な最良候補を選択 -> fresh confirm slice で再確証 -> 通れば採用
            -> KB に全候補記録。

証明する一点:
  採否は LLM/builder の自己申告ではなく、実テスト結果（tests_passed）と
  実ベンチの有意差（latency Metric の z 検定）が床。Reviewer/Judge はその床に
  必要条件を足すだけ（veto 専用）。複数提案は同一 baseline と同時比較するので
  gate は多重比較補正（Bonferroni）し、loop は 1 サイクル最大 1 採用に絞る。

Reviewer/Judge と床の関係（重要）:
  Reviewer はコード可視の欠陥を blocking で指摘し、Judge が決定的ポリシーで
  judge_approved: bool に集約する。これは evaluate_gates が tests/KPI/guardrail と
  AND する一入力に過ぎない。judge_approved=True でも tests が落ちれば不採用、
  False なら強制不採用＝レビューは採用を「止める」ことしかできず、ゲート不合格を
  救済できない。床（機械的ゲート）は動かず、必要条件が一つ増えるだけ。

多重比較・選択・確証・予算（重要）:
  K 候補を同じ baseline と比較して最良を採るのは K 回の同時検定。無補正だと family
  単位で偽陽性が膨張するため各候補を alpha/K で検定する（gate の n_comparisons）。
  さらに argmax 選択は winner's curse を残すので、選んだ winner を search に未使用の
  fresh confirm slice（別 seed）で単一比較（full alpha）し、再現したものだけ採用する
  （search ⊥ confirm）。confirm slice は有限・非定常なので各 slice に query-budget を
  課し（budget.ConfirmBudget・KB 永続）、枯渇したら confirm 不可＝採用を止める（黙って
  overfit せず「枯渇」と明示）。本当の床は外部 fresh data で、内部装置はそこに天井を残す。
"""
from __future__ import annotations

from dataclasses import dataclass

import sandbox
from budget import ConfirmBudget
from gate import GateResult, evaluate_gates
from kb import KnowledgeBase
from sandbox import DEDUPE_TASK, Task

OBJECTIVE = "dedupe_preserve_order を、正しさを保ったまま高速化する"
PRIMARY = "latency"

# workload の乱数 seed。search と confirm で別 slice を使う（search ⊥ confirm）。
_SEARCH_SEED = 1234      # sandbox.evaluate_candidate の既定と一致（探索 workload）
_CONFIRM_SEED = 99991    # 探索に一度も使わない fresh slice（confirm_winner 既定）
# confirm holdout の pool と 1 slice あたりの query-budget（longitudinal 枯渇を縛る）。
# pool 先頭は _CONFIRM_SEED。各 slice を budget 回まで使ったら次へ rotate、全枯渇で停止。
_CONFIRM_SEEDS = [99991, 99992, 99993]
_CONFIRM_BUDGET = 3


@dataclass
class CandidateResult:
    """slate 内 1 候補の評価結果。"""

    name: str
    status: str          # ADOPT(=gate 通過) / REJECT / NOT_ADOPTED
    adopt: bool          # gate.adopt（この候補単体が全ゲートを通過したか）
    tests_passed: bool
    primary_rel: float
    reasons: list
    detail: dict
    reviews: list        # 各 Reviewer の所見（review.Review）
    judge_approved: bool
    judge_reason: str
    baseline_ms: float
    candidate_ms: float


@dataclass
class CycleOutcome:
    """1 サイクル（= 1 slate）の結果。search 選択 → confirm 再確証で採否が決まる。"""

    cycle: int
    slate_size: int
    alpha_corrected: float
    results: list         # list[CandidateResult]（search 評価）
    winner: str | None    # search で選ばれた候補名（confirm 落ち/枯渇なら採用されない）
    confirmed: bool       # winner が fresh confirm slice で再現したか
    confirm_detail: dict  # confirm gate の detail（rel/p など）
    confirm_reasons: list
    confirm_seed: int | None  # 使用した confirm holdout slice（枯渇 / winner 無は None）
    exhausted: bool       # winner はいたが query-budget 枯渇で confirm できなかった
    adopted: bool         # = winner 選択 AND confirmed


@dataclass
class ConfirmResult:
    """winner を fresh confirm slice で再評価した結果。"""

    confirmed: bool
    detail: dict
    reasons: list
    baseline_ms: float
    candidate_ms: float


def _status(gate: GateResult, tests_passed: bool) -> str:
    if gate.adopt:
        return "ADOPT"
    # テスト不合格は REJECT、それ以外（主要KPI 未達 / ガードレール / Judge veto）は NOT_ADOPTED
    if not tests_passed:
        return "REJECT"
    return "NOT_ADOPTED"


def evaluate_one(candidate, reviewers: list, judge, n_comparisons: int,
                 task: Task = DEDUPE_TASK) -> CandidateResult:
    """1 候補を sandbox -> Reviewer -> Judge -> gate（Bonferroni 補正）で評価する。

    n_comparisons は slate サイズ。gate は alpha/n_comparisons で有意性を判定する。
    judge_approved は tests/KPI/guardrail と AND される必要条件（床は動かさない）。
    task は改善対象（省略時は同梱 dedupe）。KPI キーは task.primary。
    """
    sb = sandbox.evaluate_candidate(candidate.source, task)
    reviews = [r.review(candidate.name, candidate.source, sb) for r in reviewers]
    verdict = judge.decide(reviews)
    gate = evaluate_gates(
        judge_approved=verdict.approved,
        tests_passed=sb.tests_passed,
        metrics={task.primary: sb.latency},
        primary=task.primary,
        within_budget=True,
        n_comparisons=n_comparisons,         # 多重比較補正（同一 baseline・slate 同時比較）
    )
    detail = gate.detail.get(task.primary, {})
    # gate の bare "Judge 未承認" を、どの reviewer が何を指摘したかの詳細に差し替える。
    reasons = [verdict.reason if r == "Judge 未承認" else r for r in gate.reasons]
    return CandidateResult(
        name=candidate.name,
        status=_status(gate, sb.tests_passed),
        adopt=gate.adopt,
        tests_passed=sb.tests_passed,
        primary_rel=float(detail.get("rel", 0.0)),
        reasons=reasons,
        detail=gate.detail,
        reviews=reviews,
        judge_approved=verdict.approved,
        judge_reason=verdict.reason,
        baseline_ms=sb.latency.baseline_mean * 1000,
        candidate_ms=sb.latency.candidate_mean * 1000,
    )


def _select_winner(results: list, higher_is_better: bool = False):
    """gate を通った候補から主要KPI 改善が最大の 1 つを選ぶ（無ければ None）。

    改善方向に依存する: lower-is-better（latency 等）は primary_rel が最も負＝最良、
    higher-is-better（Sharpe/AUC 等）は primary_rel が最も正＝最良。1 サイクル最大 1 採用。
    """
    valid = [r for r in results if r.adopt]
    if not valid:
        return None
    return max(valid, key=lambda r: r.primary_rel) if higher_is_better \
        else min(valid, key=lambda r: r.primary_rel)


def confirm_winner(candidate, judge_approved: bool,
                   confirm_seed: int = _CONFIRM_SEED,
                   task: Task = DEDUPE_TASK) -> ConfirmResult:
    """search で選ばれた winner を fresh confirm slice（探索未使用 workload）で再評価する。

    winner's curse / search-noise への過適合を弾く。確証は単一候補なので n_comparisons=1
    （full alpha）。review は code 由来で workload 非依存なので search の判定を再利用する。
    """
    sb = sandbox.evaluate_candidate(candidate.source, task, workload_seed=confirm_seed)
    gate = evaluate_gates(
        judge_approved=judge_approved,
        tests_passed=sb.tests_passed,
        metrics={task.primary: sb.latency},
        primary=task.primary,
        within_budget=True,
        n_comparisons=1,                 # 確証は単一候補の検定（Bonferroni 不要）
    )
    return ConfirmResult(
        confirmed=gate.adopt,
        detail=gate.detail,
        reasons=gate.reasons,
        baseline_ms=sb.latency.baseline_mean * 1000,
        candidate_ms=sb.latency.candidate_mean * 1000,
    )


def run_one_cycle(builder, reviewers: list, judge, kb: KnowledgeBase,
                  cycle: int, slate_size: int = 3,
                  confirm_budget: ConfirmBudget | None = None,
                  task: Task = DEDUPE_TASK) -> CycleOutcome:
    if confirm_budget is None:
        confirm_budget = ConfirmBudget(kb, _CONFIRM_SEEDS, _CONFIRM_BUDGET)
    slate = builder.slate_for_cycle(cycle, slate_size)
    n = len(slate)   # 同時比較数 = Bonferroni の分母

    # --- search: slate を search workload で評価し valid 最良を選ぶ ---
    results = [evaluate_one(c, reviewers, judge, n, task) for c in slate]
    sel = _select_winner(results, task.higher_is_better)

    # --- confirm: winner を query-budget 内の fresh slice で再確証 ---
    #     全 slice 枯渇（spend()=None）なら confirm 不可＝採用を止める（黙って overfit しない）。
    confirmed = False
    confirm_detail: dict = {}
    confirm_reasons: list = []
    confirm_seed: int | None = None
    exhausted = False
    if sel is not None:
        confirm_seed = confirm_budget.spend()
        if confirm_seed is None:
            exhausted = True
            confirm_reasons = ["confirm holdout 枯渇（query-budget 使い切り・要 fresh data）"]
        else:
            winner_cand = next(c for c in slate if c.name == sel.name)
            cr = confirm_winner(winner_cand, sel.judge_approved,
                                confirm_seed=confirm_seed, task=task)
            confirmed = cr.confirmed
            confirm_detail = cr.detail
            confirm_reasons = cr.reasons

    adopted = sel is not None and confirmed

    # slate の全候補を KB に記録。採用フラグは最終採否。winner が confirm 落ち/枯渇なら
    # なぜ search 通過でも不採用かを reason に残す。
    objective = (OBJECTIVE if task is DEDUPE_TASK
                 else f"{task.symbol} を {task.primary} で改善")
    for cand, r in zip(slate, results):
        is_winner = sel is not None and r.name == sel.name
        reasons = list(r.reasons)
        if is_winner and not confirmed:
            tag = "confirm 枯渇" if exhausted else "confirm slice で再現せず"
            reasons.append(f"{tag}: " + "; ".join(confirm_reasons))
        kb.record(
            objective=objective,
            candidate_name=r.name,
            candidate_source=cand.source,
            adopted=(is_winner and confirmed),
            tests_passed=r.tests_passed,
            primary_rel=r.primary_rel,
            reasons=reasons,
            gate_detail=r.detail,
        )

    alpha_corrected = results[0].detail.get(task.primary, {}).get("alpha_used", 0.05) \
        if results else 0.05
    return CycleOutcome(
        cycle=cycle,
        slate_size=n,
        alpha_corrected=alpha_corrected,
        results=results,
        winner=sel.name if sel else None,
        confirmed=confirmed,
        confirm_detail=confirm_detail,
        confirm_reasons=confirm_reasons,
        confirm_seed=confirm_seed,
        exhausted=exhausted,
        adopted=adopted,
    )
