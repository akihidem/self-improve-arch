"""必要条件の不変条件を証明: Reviewer/Judge は床に必要条件を足すが床は動かさない。

採否の床（実テスト + 実ベンチの有意差）はそのままで、Reviewer/Judge は
judge_approved を **veto することしかできない**（DESIGN.md §0）。

  correct_fast   -> 全層通過 -> adopt
  unsafe_default -> ゲート単体は通す（tests+latency 合格）が、共有 mutable default を
                    Reviewer が blocking -> Judge veto -> 不採用（必要条件が止める）
  wrong_fast     -> judge_approved=True を強制しても tests 不合格で不採用（床は不動）
"""
import sandbox
from builder import MOCK_CANDIDATES
from gate import evaluate_gates
from review import Judge, Review, ReviewerMock, make_reviewers


def _sandbox(name):
    return sandbox.evaluate_candidate(MOCK_CANDIDATES[name].source)


def _review_and_judge(name, sb):
    reviewers = make_reviewers("mock")
    reviews = [r.review(name, MOCK_CANDIDATES[name].source, sb) for r in reviewers]
    return reviews, Judge().decide(reviews)


def test_clean_candidate_approved_by_all_layers():
    sb = _sandbox("correct_fast")
    reviews, verdict = _review_and_judge("correct_fast", sb)
    assert all(r.approve for r in reviews), [r.blocking for r in reviews]
    assert verdict.approved is True
    gate = evaluate_gates(judge_approved=verdict.approved, tests_passed=sb.tests_passed,
                          metrics={"latency": sb.latency}, primary="latency")
    assert gate.adopt is True, gate.reasons


def test_unsafe_default_passes_gate_floor_but_review_vetoes():
    """核: ゲート単体は非 reentrant 実装を ADOPT する（盲目）。
    Reviewer/Judge を足すと veto され不採用になる = 必要条件が一つ増えた。"""
    sb = _sandbox("unsafe_default")

    # (a) sandbox 実測は緑: tests 合格 + 主要KPI 有意改善 → ゲート単体なら ADOPT
    assert sb.tests_passed is True, sb.test_output
    gate_floor = evaluate_gates(judge_approved=True, tests_passed=sb.tests_passed,
                                metrics={"latency": sb.latency}, primary="latency")
    assert gate_floor.adopt is True, ("gate 単体は非 reentrant を通すはず", gate_floor.reasons)

    # (b) Reviewer 2体 -> Judge では veto。両者が別観点から blocking する。
    reviews, verdict = _review_and_judge("unsafe_default", sb)
    assert verdict.approved is False
    roles_blocking = {r.role for r in reviews if r.blocking}
    assert roles_blocking == {"safety", "scope"}, roles_blocking

    gate = evaluate_gates(judge_approved=verdict.approved, tests_passed=sb.tests_passed,
                          metrics={"latency": sb.latency}, primary="latency")
    assert gate.adopt is False
    assert any("Judge 未承認" in r for r in gate.reasons), gate.reasons


def test_judge_cannot_rescue_failing_gate():
    """床は動かない: tests 不合格は judge_approved=True を強制しても不採用。"""
    sb = _sandbox("wrong_fast")
    assert sb.tests_passed is False
    gate = evaluate_gates(judge_approved=True, tests_passed=sb.tests_passed,
                          metrics={"latency": sb.latency}, primary="latency")
    assert gate.adopt is False
    assert any("テスト不合格" in r for r in gate.reasons), gate.reasons


def test_judge_is_veto_only():
    """Judge は全員 approve でのみ承認、blocking が一つでもあれば不承認。"""
    judge = Judge()
    ok = [Review("a", "safety", True), Review("b", "scope", True)]
    assert judge.decide(ok).approved is True

    bad = [Review("a", "safety", False, blocking=["x"]), Review("b", "scope", True)]
    v = judge.decide(bad)
    assert v.approved is False
    assert "x" in v.reason


def test_reviewer_mock_is_deterministic():
    src = MOCK_CANDIDATES["unsafe_default"].source
    r = ReviewerMock("safety")
    a = r.review("unsafe_default", src)
    b = r.review("unsafe_default", src)
    assert a.approve is False and b.approve is False
    assert a.blocking == b.blocking
    assert len(a.blocking) == 1
