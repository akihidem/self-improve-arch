"""『実際に使える』回帰: 任意ターゲット(first_unique 例) × 実候補ファイル(BuilderDir)で動くこと。

dedupe 以外のターゲットに向けられること（Task で module/symbol/target_dir 差し替え）と、
operator が用意した候補ファイル群を BuilderDir で読み full rigor で採否できることを固定する。
"""
from pathlib import Path

import sandbox
from builder import BuilderDir, Candidate
from gate import evaluate_gates
from sandbox import Task

_HERE = Path(__file__).resolve().parent
_EX = _HERE / "examples" / "first_unique"
_CANDS = _HERE / "examples" / "first_unique_candidates"
_TASK = Task(target_dir=_EX, module="first_unique", symbol="first_unique", primary="latency")


def _adopt(name: str) -> bool:
    src = (_CANDS / f"{name}.py").read_text(encoding="utf-8")
    sb = sandbox.evaluate_candidate(src, _TASK)
    g = evaluate_gates(judge_approved=True, tests_passed=sb.tests_passed,
                       metrics={"latency": sb.latency}, primary="latency")
    return sb, g


def test_builder_dir_reads_candidate_files():
    slate = BuilderDir(_CANDS).slate_for_cycle(1)
    names = sorted(c.name for c in slate)
    assert names == ["correct_fast", "noop", "wrong"], names
    assert all(isinstance(c, Candidate) and "first_unique" in c.source for c in slate)


def test_generalized_target_correct_fast_adopts():
    sb, g = _adopt("correct_fast")
    assert sb.tests_passed is True, sb.test_output
    assert g.adopt is True, (g.reasons, g.detail)   # 正しく O(n) = 採用水準


def test_generalized_target_wrong_rejected_on_tests():
    sb, g = _adopt("wrong")
    assert sb.tests_passed is False    # 最後の一意を返す＝first 契約違反でテスト落ち
    assert g.adopt is False


def test_generalized_target_noop_not_adopted():
    sb, g = _adopt("noop")
    assert sb.tests_passed is True     # baseline と同じ O(n^2)＝正しいが
    assert g.adopt is False            # 改善なしで不採用


def test_reviewers_generalize_to_target_symbol():
    """Reviewer が task.symbol に追従し、別ターゲットの valid 候補を誤 veto しない（回帰）。"""
    from review import Judge, make_reviewers

    src = (_CANDS / "correct_fast.py").read_text(encoding="utf-8")
    reviewers = make_reviewers("mock", _TASK.symbol, _TASK.baseline_params)
    reviews = [r.review("correct_fast", src) for r in reviewers]
    assert all(not rv.blocking for rv in reviews), [rv.blocking for rv in reviews]
    assert Judge().decide(reviews).approved is True
