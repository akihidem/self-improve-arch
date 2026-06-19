#!/usr/bin/env python3
"""self-improvement loop の実装例（説明用・mock で単一/複数サイクルが動く）。

先行する「生成 → 検証 → 判断」の決定的オーケストレータを、本番 Web サービスの
継続的自己改善向けに拡張した最小例。新規要素は次の 3 点:
  1) KPI ゲート（統計的有意 + ガードレール非回帰）— 採否判定の核
  2) Reviewer 2 体の並列実行 + Judge による統合
  3) Knowledge Base（履歴保存・参照）

中核方針:
  採否は LLM の自己申告では決めない。テスト実測・KPI 実測・ガードレールという
  「機械的ゲート」が床。Judge の承認は必要条件であって十分条件ではない。

権限境界（重要）:
  本番への適用はこのコードの責務外（Ring2 = 人間承認 + 別パイプライン）。
  このループは Sandbox 内 baseline の更新（Ring1）までしか行わない。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import statistics
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

# ===========================================================================
# LLM 抽象（mock）。本番は Claude / Ollama 等へ差し替える。
# Reviewer は「別モデル・別観点」で独立性を確保するのが望ましい（後述）。
# ===========================================================================


class LLMClient:
    def complete(self, system: str, user: str, *, role: str = "") -> str:
        raise NotImplementedError


class MockLLM(LLMClient):
    """役割ごとに決め打ち JSON を返す。cycle で挙動を変え、採用/不採用の両経路を示す。

    注意（2026-06-19 検証）: complete() は system/user プロンプトを無視して固定 JSON を
    返す。よって RAG（kb.query_similar → builder.propose がプロンプトに ctx を埋込）は
    配線済みだが **ctx は提案に作用しない（wired-but-inert）**。前例複利（過去の採否が
    次の提案を変える）の挙動は実 LLM でのみ現れ、この例では実証していない。
    """

    def __init__(self) -> None:
        self.cycle = 0

    def complete(self, system: str, user: str, *, role: str = "") -> str:
        if role == "builder":
            return json.dumps(
                {
                    "diff": "--- a/handler.py\n+++ b/handler.py\n@@\n- cache=None\n+ cache=LRU(512)",
                    "hypothesis": "応答キャッシュ導入で p50 latency を下げる",
                    # 事前宣言（pre-registration）: 期待効果を先に固定し p-hacking を抑止
                    "expected_primary_rel": -0.15,
                    "touched": ["handler.py"],
                },
                ensure_ascii=False,
            )
        if role in ("reviewer_a", "reviewer_b"):
            lens = "正しさ・回帰リスク" if role == "reviewer_a" else "価値・設計健全性"
            return json.dumps(
                {
                    "verdict": "approve",
                    "risk": 0.2,
                    "findings": [f"{lens}: 重大な問題なし。キャッシュ無効化条件のテストを必須化。"],
                },
                ensure_ascii=False,
            )
        if role == "judge":
            return json.dumps(
                {"approved": True, "rationale": "2 レビューが独立に承認。無効化テスト追加を条件に Sandbox 適用可。"},
                ensure_ascii=False,
            )
        return "{}"


# ===========================================================================
# データ構造
# ===========================================================================


@dataclass
class Proposal:
    diff: str
    hypothesis: str
    expected_primary_rel: float
    touched: list
    raw: str = ""


@dataclass
class Review:
    reviewer: str
    verdict: str
    risk: float
    findings: list
    raw: str = ""


@dataclass
class Decision:
    approved: bool
    rationale: str
    raw: str = ""


@dataclass
class Metric:
    """KPI の baseline vs candidate 計測値（要約統計）。"""

    name: str
    baseline_mean: float
    baseline_std: float
    candidate_mean: float
    candidate_std: float
    n: int
    higher_is_better: bool
    is_guardrail: bool = False


@dataclass
class GateResult:
    adopt: bool
    reasons: list
    detail: dict = field(default_factory=dict)


# ===========================================================================
# エージェント
# ===========================================================================


class Builder:
    def __init__(self, llm: LLMClient) -> None:
        self.llm = llm

    def propose(self, objective: str, kb_context: list) -> Proposal:
        raw = self.llm.complete(
            "You are the Builder.",
            f"目的: {objective}\n過去の関連実験: {json.dumps(kb_context, ensure_ascii=False)}",
            role="builder",
        )
        d = json.loads(raw)
        return Proposal(
            diff=d["diff"],
            hypothesis=d["hypothesis"],
            expected_primary_rel=float(d["expected_primary_rel"]),
            touched=list(d.get("touched", [])),
            raw=raw,
        )


class Reviewer:
    def __init__(self, llm: LLMClient, name: str, role_key: str) -> None:
        self.llm = llm
        self.name = name
        self.role_key = role_key

    def review(self, proposal: Proposal) -> Review:
        raw = self.llm.complete(
            f"You are Reviewer {self.name}.",
            f"差分:\n{proposal.diff}\n仮説: {proposal.hypothesis}",
            role=self.role_key,
        )
        d = json.loads(raw)
        return Review(
            reviewer=self.name,
            verdict=str(d.get("verdict", "reject")),
            risk=float(d.get("risk", 1.0)),
            findings=list(d.get("findings", [])),
            raw=raw,
        )


class Judge:
    def __init__(self, llm: LLMClient) -> None:
        self.llm = llm

    def decide(self, reviews: list, proposal: Proposal) -> Decision:
        # 統合の最低条件は機械的に課す: 全 reviewer が approve でなければ即却下。
        # （LLM の言い分より先に、満たすべき下限をコードで固定する。）
        if not all(r.verdict == "approve" for r in reviews):
            return Decision(approved=False, rationale="reviewer のいずれかが非承認", raw="")
        raw = self.llm.complete(
            "You are the Judge.",
            "レビュー: " + json.dumps([r.findings for r in reviews], ensure_ascii=False),
            role="judge",
        )
        d = json.loads(raw)
        return Decision(approved=bool(d.get("approved", False)), rationale=str(d.get("rationale", "")), raw=raw)


# ===========================================================================
# KPI ゲート（採否判定の核。LLM に依存しない決定的ロジック）
# ===========================================================================


def two_sample_z(b_mean, b_std, c_mean, c_std, n):
    """大標本の正規近似による両側 z 検定（stdlib のみ）。

    注意: これは簡略版。本番は逐次検定（mSPRT 等）/ 適切な統計ライブラリ /
    多重比較補正（複数提案を同一 baseline と比較する場合）を用いること。
    """
    se = ((b_std ** 2) / n + (c_std ** 2) / n) ** 0.5
    if se == 0:
        return 0.0, 1.0
    z = (c_mean - b_mean) / se
    p = 2 * (1 - statistics.NormalDist().cdf(abs(z)))
    return z, p


def evaluate_gates(
    *,
    judge_approved: bool,
    tests_passed: bool,
    metrics: dict,
    primary: str,
    within_budget: bool = True,
    alpha: float = 0.05,
    min_effect: float = 0.05,
    guardrail_tol: float = 0.02,
) -> GateResult:
    """採用 = Judge承認 AND テスト合格 AND 予算内 AND 主要KPIが有意改善 AND
    ガードレール非回帰。1 つでも欠ければ不採用とし、理由を残す。"""
    reasons: list = []
    detail: dict = {}

    if not tests_passed:
        reasons.append("テスト不合格")
    if not judge_approved:
        reasons.append("Judge 未承認")
    if not within_budget:
        reasons.append("予算超過")

    # 主要 KPI: 改善方向 かつ 有意 かつ 効果量が閾値以上
    pm = metrics[primary]
    z, p = two_sample_z(pm.baseline_mean, pm.baseline_std, pm.candidate_mean, pm.candidate_std, pm.n)
    rel = (pm.candidate_mean - pm.baseline_mean) / abs(pm.baseline_mean) if pm.baseline_mean else 0.0
    improved = rel > 0 if pm.higher_is_better else rel < 0
    significant = p < alpha
    big_enough = abs(rel) >= min_effect
    detail[primary] = {
        "rel": round(rel, 4), "p": round(p, 6), "z": round(z, 3),
        "improved": improved, "significant": significant, "big_enough": big_enough,
    }
    if not (improved and significant and big_enough):
        reasons.append(f"主要KPI {primary} が改善基準未達 (rel={rel:.3f}, p={p:.4g})")

    # ガードレール（counter-metric）: 1 つでも許容を超えて悪化したら不採用 → Goodhart 対策
    for name, m in metrics.items():
        if not m.is_guardrail:
            continue
        grel = (m.candidate_mean - m.baseline_mean) / abs(m.baseline_mean) if m.baseline_mean else 0.0
        regressed = (grel < -guardrail_tol) if m.higher_is_better else (grel > guardrail_tol)
        detail[name] = {"rel": round(grel, 4), "regressed": regressed, "guardrail": True}
        if regressed:
            reasons.append(f"ガードレール {name} が回帰 (rel={grel:.3f})")

    adopt = len(reasons) == 0
    return GateResult(adopt=adopt, reasons=(["全ゲート通過"] if adopt else reasons), detail=detail)


# ===========================================================================
# Knowledge Base（参考実装: sqlite。本番は Postgres + pgvector + S3/MinIO）
# ===========================================================================

SCHEMA = """
CREATE TABLE IF NOT EXISTS experiments(
  id INTEGER PRIMARY KEY, objective TEXT, hypothesis TEXT,
  diff_sha TEXT, expected_rel REAL, created REAL);
CREATE TABLE IF NOT EXISTS decisions(
  exp_id INTEGER, adopted INTEGER, reason TEXT, gate_detail TEXT,
  promoted_to_prod INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS artifacts(sha TEXT PRIMARY KEY, body TEXT);
"""


class KnowledgeBase:
    def __init__(self, path: str = ":memory:") -> None:
        self.cx = sqlite3.connect(path)
        self.cx.executescript(SCHEMA)

    @staticmethod
    def _sha(text: str) -> str:
        return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]

    def query_similar(self, objective: str, limit: int = 3) -> list:
        # 参考実装: 文字列一致。本番はベクトル検索（pgvector / Qdrant）で意味的に検索。
        cur = self.cx.execute(
            "SELECT e.objective, d.adopted, d.reason FROM experiments e "
            "LEFT JOIN decisions d ON d.exp_id=e.id ORDER BY e.id DESC LIMIT ?",
            (limit,),
        )
        return [{"objective": o, "adopted": bool(a) if a is not None else None, "reason": r} for o, a, r in cur.fetchall()]

    def record(self, objective, proposal: Proposal, gate: GateResult, adopted: bool) -> int:
        diff_sha = self._sha(proposal.diff)
        self.cx.execute("INSERT OR IGNORE INTO artifacts(sha, body) VALUES(?,?)", (diff_sha, proposal.diff))
        cur = self.cx.execute(
            "INSERT INTO experiments(objective, hypothesis, diff_sha, expected_rel, created) VALUES(?,?,?,?,?)",
            (objective, proposal.hypothesis, diff_sha, proposal.expected_primary_rel, __import__("time").time()),
        )
        exp_id = cur.lastrowid
        self.cx.execute(
            "INSERT INTO decisions(exp_id, adopted, reason, gate_detail) VALUES(?,?,?,?)",
            (exp_id, int(adopted), "; ".join(gate.reasons), json.dumps(gate.detail, ensure_ascii=False)),
        )
        self.cx.commit()
        return exp_id


# ===========================================================================
# Improvement Controller（統合 AI / オーケストレータ相当）
# ===========================================================================


@dataclass
class Budget:
    max_cycles: int = 10
    used: int = 0

    def ok(self) -> bool:
        return self.used < self.max_cycles


class ImprovementController:
    def __init__(self, llm: LLMClient, kb: KnowledgeBase, budget: Budget) -> None:
        self.llm = llm
        self.kb = kb
        self.budget = budget
        self.builder = Builder(llm)
        self.reviewer_a = Reviewer(llm, "A", "reviewer_a")  # 観点A: 正しさ・安全
        self.reviewer_b = Reviewer(llm, "B", "reviewer_b")  # 観点B: 価値・設計
        self.judge = Judge(llm)

    # --- Sandbox 内の機械的検証（mock） ---
    def _run_sandbox_tests(self, proposal: Proposal) -> bool:
        return True  # mock: 実体は CI のテストジョブ結果（JUnit）を読む

    def _measure_kpi(self, proposal: Proposal, cycle: int) -> dict:
        """候補 vs baseline の KPI 計測（mock）。
        cycle 1 は健全な改善、cycle 2 は「主要KPIは改善するがガードレール回帰」の例。
        """
        if cycle == 1:
            return {
                "latency_p50": Metric("latency_p50", 200, 30, 170, 28, 2000, higher_is_better=False),
                "error_rate": Metric("error_rate", 0.005, 0.001, 0.005, 0.001, 2000, higher_is_better=False, is_guardrail=True),
                "latency_p99": Metric("latency_p99", 800, 60, 790, 58, 2000, higher_is_better=False, is_guardrail=True),
                "cost_per_req": Metric("cost_per_req", 1.00, 0.05, 1.01, 0.05, 2000, higher_is_better=False, is_guardrail=True),
            }
        return {  # latency は改善するが error_rate が悪化 → ガードレールが採用を止める
            "latency_p50": Metric("latency_p50", 200, 30, 160, 27, 2000, higher_is_better=False),
            "error_rate": Metric("error_rate", 0.005, 0.001, 0.020, 0.002, 2000, higher_is_better=False, is_guardrail=True),
            "latency_p99": Metric("latency_p99", 800, 60, 780, 58, 2000, higher_is_better=False, is_guardrail=True),
            "cost_per_req": Metric("cost_per_req", 1.00, 0.05, 1.00, 0.05, 2000, higher_is_better=False, is_guardrail=True),
        }

    def run_cycle(self, objective: str) -> dict:
        if not self.budget.ok():
            return {"status": "BUDGET_EXCEEDED"}
        self.budget.used += 1
        cycle = self.budget.used

        ctx = self.kb.query_similar(objective)
        proposal = self.builder.propose(objective, ctx)

        # 2 体のレビューを並列実行
        with ThreadPoolExecutor(max_workers=2) as ex:
            fa = ex.submit(self.reviewer_a.review, proposal)
            fb = ex.submit(self.reviewer_b.review, proposal)
            reviews = [fa.result(), fb.result()]

        decision = self.judge.decide(reviews, proposal)
        if not decision.approved:
            gate = GateResult(adopt=False, reasons=["Judge 未承認: " + decision.rationale])
            self.kb.record(objective, proposal, gate, adopted=False)
            return {"status": "REJECTED_BY_JUDGE", "cycle": cycle, "reasons": gate.reasons}

        # --- ここから Sandbox 内（Ring1）。本番(Ring2)は別系統・人間承認 ---
        tests_passed = self._run_sandbox_tests(proposal)
        metrics = self._measure_kpi(proposal, cycle)
        gate = evaluate_gates(
            judge_approved=True,
            tests_passed=tests_passed,
            metrics=metrics,
            primary="latency_p50",
            within_budget=self.budget.ok(),
        )
        self.kb.record(objective, proposal, gate, adopted=gate.adopt)
        return {
            "status": "ADOPTED_IN_SANDBOX" if gate.adopt else "NOT_ADOPTED",
            "cycle": cycle,
            "reasons": gate.reasons,
            "detail": gate.detail,
        }


# ===========================================================================
# デモ: mock で 2 サイクル回す
# ===========================================================================

if __name__ == "__main__":
    kb = KnowledgeBase(":memory:")
    ctrl = ImprovementController(MockLLM(), kb, Budget(max_cycles=10))

    for _ in range(2):
        result = ctrl.run_cycle("p50 latency を下げる")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        print("-" * 60)

    print("KB に記録された実験:")
    for row in kb.query_similar("", limit=10):
        print(" ", row)
