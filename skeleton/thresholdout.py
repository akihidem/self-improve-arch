"""Thresholdout（Dwork et al. 2015）を confirm holdout の再利用に適応する.

背景: 素朴な query-budget（budget.py）は confirm slice を叩くたびに 1 消費し、枯渇したら
停止する。Thresholdout は **「search 推定と confirm holdout が一致するクエリは holdout を
ほとんど消費しない」** ことで、adaptive に投げられるクエリ回数を延ばす。

仕組み（このリポへの適応）:
  - train 推定 = search slice の rel（候補の相対改善）。holdout = confirm slice の rel。
  - ノイズ閾値 T̂ = threshold + Laplace。|holdout - search| にもノイズを足して T̂ と比較。
  - 一致（差 ≤ T̂）: holdout を「開示しない」＝search 判定を返す（**privacy budget 不消費**）。
  - 乖離（差 > T̂）: 過学習シグナル。noisy な holdout を開示し privacy budget を 1 消費。
  - surprising（乖離）クエリだけが budget を食う → 一致が続く限り holdout は延命する。

honest（残る天井）: これは情報を増やさない。threshold/sigma は統計妥当性とクエリ数の
トレードオフ。surprising クエリの budget も有限で、枯渇したら結局 **genuinely fresh data**
が要る（[[budget.py]] / RSI-LIMITS と同じ内部測定の天井）。ノイズは wall-clock でなく
(seed, query_index) から決定的に引く（再現性・監査可能性のため）。
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass


@dataclass(frozen=True)
class ThresholdoutVerdict:
    reported_rel: float    # 採否に使う rel（一致時=search、乖離時=noisy holdout）
    used_holdout: bool     # holdout を実際に開示したか（= 過学習シグナル検出）
    overfit: bool          # ノイズ閾値超え（search と confirm が食い違う）
    exhausted: bool        # privacy budget 枯渇（これ以上 confirm 不可）
    note: str


def _laplace(rng: random.Random, scale: float) -> float:
    """逆 CDF で Laplace(0, scale) を引く（rng は決定的）。"""
    if scale <= 0:
        return 0.0
    u = rng.random() - 0.5
    return -scale * math.copysign(1.0, u) * math.log(1.0 - 2.0 * abs(u))


class Thresholdout:
    """confirm holdout 再利用ポリシー。状態（消費回数）は spend_fn 側で永続させる。

    threshold: search と holdout の rel 差をこの範囲なら「一致」とみなす許容幅。
    sigma:     Laplace ノイズ尺度（大きいほど holdout を守るがクエリは粗くなる）。
    seed:      ノイズの決定的シード（再現性）。
    """

    def __init__(self, *, threshold: float = 0.15, sigma: float = 0.05, seed: int = 99991,
                 budget: int = 8) -> None:
        if threshold < 0 or sigma < 0:
            raise ValueError("threshold / sigma は非負")
        if budget < 1:
            raise ValueError("budget は 1 以上")
        self.threshold = threshold
        self.sigma = sigma
        self.seed = seed
        self.budget = budget
        self._spent = 0   # 開示（surprising）回数。in-memory（1 run 内）。
        self._qi = 0      # 単調クエリカウンタ（ノイズの決定的シード源）。

    def assess_auto(self, search_rel: float, holdout_rel: float) -> ThresholdoutVerdict:
        """内部 budget / query カウンタで assess() を回す自己完結版（loop 配線用）。

        cross-run 永続が要るなら ConfirmBudget と同様に KB-backed の spend_fn を assess() へ
        渡すこと（本メソッドは 1 run 内 in-memory）。
        """
        qi = self._qi
        self._qi += 1

        def _spend():
            if self._spent >= self.budget:
                return None
            self._spent += 1
            return self._spent

        return self.assess(search_rel, holdout_rel, qi, _spend)

    def status(self) -> dict:
        return {"spent": self._spent, "budget": self.budget, "queries": self._qi}

    def assess(self, search_rel: float, holdout_rel: float, query_index: int,
               spend_fn) -> ThresholdoutVerdict:
        """1 回の confirm を判定する。

        spend_fn(): 過学習シグナル時に呼ぶ。残予算があれば消費し index(int) を返す。
                    枯渇なら None を返す（= これ以上 holdout を開示できない）。
        query_index: ノイズを決定的に引くための単調増加カウンタ（KB の消費数など）。
        """
        rng = random.Random(f"{self.seed}:{int(query_index)}")
        t_hat = self.threshold + _laplace(rng, 2.0 * self.sigma)
        noisy_gap = abs(holdout_rel - search_rel) + _laplace(rng, self.sigma)

        if noisy_gap <= t_hat:
            # 一致 → holdout を開示せず search 判定を返す（budget 不消費）。
            return ThresholdoutVerdict(
                reported_rel=search_rel, used_holdout=False, overfit=False,
                exhausted=False,
                note=f"一致（noisy_gap={noisy_gap:.3f} ≤ T̂={t_hat:.3f}）→ holdout 温存・search 判定")

        # 乖離 = 過学習シグナル。budget を消費して noisy holdout を開示する。
        if spend_fn() is None:
            return ThresholdoutVerdict(
                reported_rel=search_rel, used_holdout=False, overfit=True,
                exhausted=True,
                note="過学習シグナルだが privacy budget 枯渇 → confirm 不可（要 fresh data）")
        noisy_holdout = holdout_rel + _laplace(rng, self.sigma)
        return ThresholdoutVerdict(
            reported_rel=noisy_holdout, used_holdout=True, overfit=True,
            exhausted=False,
            note=f"乖離（noisy_gap={noisy_gap:.3f} > T̂={t_hat:.3f}）→ noisy holdout={noisy_holdout:.3f} 開示・budget 消費")
