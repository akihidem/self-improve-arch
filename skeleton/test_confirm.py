"""fresh confirm slice（search ⊥ confirm）: winner's curse を弾く確証フェーズの検証。

  - confirm slice は search と別 workload（探索に未使用）である。
  - confirm ゲートは単一比較（n_comparisons=1・full alpha）で、search で勝っても
    effect が消えれば不採用にする（winner's curse / search-noise への過適合を弾く）。
  - 真の O(n) winner は fresh slice でも有意に再現し confirmed=True。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "target"))

import loop
from bench import make_workload
from builder import MOCK_CANDIDATES
from gate import Metric, evaluate_gates
from loop import confirm_winner


def test_confirm_seed_differs_from_search():
    assert loop._CONFIRM_SEED != loop._SEARCH_SEED


def test_fresh_confirm_uses_distinct_workload():
    """confirm slice は search と別 workload（探索に未使用）であることを保証。"""
    search = make_workload(seed=loop._SEARCH_SEED)
    confirm = make_workload(seed=loop._CONFIRM_SEED)
    assert search != confirm
    assert len(search) == len(confirm)   # 構造（サイズ）は同じ・中身だけ別 slice


def test_confirm_gate_rejects_when_effect_vanishes():
    """search で勝っても confirm で効果が消えれば（rel≈0・非有意）採用しない。

    confirm は単一候補なので n_comparisons=1（full alpha）。それでも改善が無ければ落ちる。
    """
    vanished = Metric(name="latency", baseline_mean=1.0, baseline_std=0.1,
                      candidate_mean=1.0, candidate_std=0.1, n=30, higher_is_better=False)
    gate = evaluate_gates(judge_approved=True, tests_passed=True,
                          metrics={"latency": vanished}, primary="latency", n_comparisons=1)
    assert gate.adopt is False
    assert gate.detail["latency"]["significant"] is False
    assert any("主要KPI" in r for r in gate.reasons), gate.reasons


def test_confirm_accepts_genuine_winner():
    """真の O(n) winner は fresh confirm slice でも有意に再現し confirmed=True。"""
    cr = confirm_winner(MOCK_CANDIDATES["correct_fast"], judge_approved=True)
    assert cr.confirmed is True, cr.reasons
    assert cr.detail["latency"]["significant"] is True
    assert cr.candidate_ms < cr.baseline_ms   # 高速化が fresh slice でも出ている
