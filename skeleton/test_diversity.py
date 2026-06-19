"""候補の多様性: 多重比較補正と「1 サイクル最大 1 採用」の選択ロジックを検証。

  - Bonferroni 補正: marginal な効果（p≈0.03）は単一比較なら有意だが、slate=5 の
    同時比較（alpha/5=0.01）では有意でなくなり不採用になる。タイミング非依存に証明。
  - _select_winner: gate を通った候補から主要KPI 改善最大を 1 つ選ぶ／無ければ None。
  - 新候補 correct_dict（dict.fromkeys）が valid（tests 合格・有意改善・review 無 blocking）。
"""
import sandbox
from builder import MOCK_CANDIDATES
from gate import Metric, evaluate_gates, two_sample_z
from loop import CandidateResult, _select_winner
from review import make_reviewers


def _marginal_metric() -> Metric:
    """p≈0.03 になる合成 latency Metric（rel=-0.10・lower better）。

    se = sqrt(2*std^2/n)。std=0.18, n=30 で z≈-2.15 → p≈0.031（0.01〜0.05 の間）。
    """
    return Metric(
        name="latency", baseline_mean=1.0, baseline_std=0.18,
        candidate_mean=0.9, candidate_std=0.18, n=30, higher_is_better=False,
    )


def test_bonferroni_correction_flips_significance():
    m = _marginal_metric()
    _, p = two_sample_z(m.baseline_mean, m.baseline_std, m.candidate_mean,
                        m.candidate_std, m.n)
    # marginal: 単一比較の閾値は跨ぐが、slate=5 の補正閾値（0.01）は跨がない
    assert 0.05 / 5 < p < 0.05, p

    g1 = evaluate_gates(judge_approved=True, tests_passed=True,
                        metrics={"latency": m}, primary="latency", n_comparisons=1)
    assert g1.detail["latency"]["significant"] is True
    assert g1.detail["latency"]["alpha_used"] == 0.05
    assert g1.adopt is True, g1.reasons

    g5 = evaluate_gates(judge_approved=True, tests_passed=True,
                        metrics={"latency": m}, primary="latency", n_comparisons=5)
    assert g5.detail["latency"]["significant"] is False
    assert g5.detail["latency"]["alpha_used"] == 0.01
    assert g5.adopt is False
    assert any("主要KPI" in r for r in g5.reasons), g5.reasons


def _cr(name: str, adopt: bool, rel: float) -> CandidateResult:
    return CandidateResult(
        name=name, status="ADOPT" if adopt else "NOT_ADOPTED", adopt=adopt,
        tests_passed=True, primary_rel=rel, reasons=[], detail={}, reviews=[],
        judge_approved=True, judge_reason="", baseline_ms=1.0, candidate_ms=1.0,
    )


def test_select_winner_picks_best_valid():
    # c は最大改善(-0.99)だが gate 不通過なので対象外。adopt 中の最良 b(-0.9) を選ぶ。
    results = [_cr("a", True, -0.5), _cr("b", True, -0.9), _cr("c", False, -0.99)]
    winner = _select_winner(results)
    assert winner is not None and winner.name == "b"


def test_select_winner_higher_is_better_picks_max():
    # higher-is-better（Sharpe/AUC 等）は primary_rel が最大の valid を選ぶ。
    # c は rel 最大(2.0)だが gate 不通過なので対象外 → 残る a(0.9)/b(1.5) から b。
    results = [_cr("a", True, 0.9), _cr("b", True, 1.5), _cr("c", False, 2.0)]
    winner = _select_winner(results, higher_is_better=True)
    assert winner is not None and winner.name == "b"


def test_select_winner_none_when_no_valid():
    results = [_cr("x", False, -0.9), _cr("y", False, -0.8)]
    assert _select_winner(results) is None


def test_correct_dict_is_valid_and_vetoless():
    src = MOCK_CANDIDATES["correct_dict"].source
    sb = sandbox.evaluate_candidate(src)
    assert sb.tests_passed is True, sb.test_output

    reviews = [r.review("correct_dict", src, sb) for r in make_reviewers("mock")]
    assert all(not rv.blocking for rv in reviews), [rv.blocking for rv in reviews]

    # 真の O(n) 改善は z が巨大なので slate=3 の Bonferroni 補正でも有意（補正は噛まない）
    gate = evaluate_gates(judge_approved=True, tests_passed=sb.tests_passed,
                          metrics={"latency": sb.latency}, primary="latency", n_comparisons=3)
    assert gate.adopt is True, gate.reasons
