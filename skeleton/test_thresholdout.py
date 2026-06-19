"""Thresholdout の回帰テスト.

核となる性質を決定的に検証する:
  - search と holdout が一致するクエリは privacy budget を消費しない（= holdout 延命）。
  - 大きく乖離するクエリ（過学習シグナル）だけが budget を消費し noisy holdout を開示する。
  - budget 枯渇後は confirm 不可（exhausted）。
  - ノイズは (seed, query_index) から決定的＝再現性あり。
"""
from __future__ import annotations

from thresholdout import Thresholdout, _laplace
import random


def _budget(n: int):
    """残 n 回の spend_fn。呼ぶたび消費し index を返す。枯渇で None。"""
    state = {"left": n, "calls": 0}
    def spend():
        state["calls"] += 1
        if state["left"] <= 0:
            return None
        state["left"] -= 1
        return n - state["left"]
    return spend, state


def test_huge_gap_always_overfit_and_spends():
    # search と holdout が大きく食い違う（過学習）→ 必ず overfit・budget 消費・noisy holdout 開示。
    to = Thresholdout(threshold=0.15, sigma=0.05)
    spend, st = _budget(100)
    overfit = 0
    for qi in range(20):
        v = to.assess(search_rel=-0.90, holdout_rel=0.50, query_index=qi, spend_fn=spend)
        if v.overfit and v.used_holdout:
            overfit += 1
            assert abs(v.reported_rel - 0.50) < 0.4   # noisy holdout（≈holdout_rel）
    assert overfit == 20                                # gap=1.4 ≫ ノイズ → 全部 overfit
    assert st["calls"] == 20                            # 毎回 budget を消費


def test_zero_gap_mostly_consistent_preserves_budget():
    # search と holdout がほぼ一致 → 大半は holdout 不開示・budget 不消費（延命）。
    to = Thresholdout(threshold=0.15, sigma=0.05)
    spend, st = _budget(100)
    consistent = 0
    for qi in range(20):
        v = to.assess(search_rel=-0.90, holdout_rel=-0.90, query_index=qi, spend_fn=spend)
        if not v.used_holdout and not v.overfit:
            consistent += 1
            assert v.reported_rel == -0.90              # search 判定をそのまま返す
    assert consistent >= 14                             # 大多数が一致（budget を温存）
    assert st["calls"] <= 6                             # 消費は surprising クエリのみ


def test_exhaustion_blocks_confirm():
    # budget 0 で過学習シグナル → exhausted・holdout 開示せず・search_rel を返す。
    to = Thresholdout(threshold=0.15, sigma=0.05)
    spend, _ = _budget(0)
    v = to.assess(search_rel=-0.90, holdout_rel=0.50, query_index=0, spend_fn=spend)
    assert v.exhausted and not v.used_holdout and v.reported_rel == -0.90


def test_deterministic_across_instances():
    # 同 seed・同入力 → 同じ判定（再現性）。
    spend1, _ = _budget(100)
    spend2, _ = _budget(100)
    a = Thresholdout(seed=42).assess(-0.8, 0.3, 7, spend1)
    b = Thresholdout(seed=42).assess(-0.8, 0.3, 7, spend2)
    assert (a.reported_rel, a.overfit, a.used_holdout) == (b.reported_rel, b.overfit, b.used_holdout)


def test_laplace_zero_scale():
    assert _laplace(random.Random(0), 0.0) == 0.0
