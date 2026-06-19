"""first_unique の実ベンチ（latency, lower better）。dedupe の bench と同じ規約。

make_workload: 大リスト（多くが重複・末尾に唯一の一意要素）。baseline は items.count を
毎回呼ぶ O(n^2) なので、一意要素が末尾＝最後まで count し続け、O(n) 候補との差が大きく出る。
measure_interleaved: baseline/candidate を同一プロセスで交互計測（系統差を相殺）。
"""
from __future__ import annotations

import time

# 計測クロックを束縛退避（候補の time.perf_counter 差し替えに耐える。境界ではない・sandbox.py 参照）。
_perf_counter = time.perf_counter


def make_workload(size=3000, distinct=150, seed=1234):
    # 決定的な多重複リスト + 末尾に唯一の一意要素（baseline が末尾まで count する）。
    import random

    rng = random.Random(seed)
    data = [rng.randrange(distinct) for _ in range(size - 1)]
    data.append(distinct + 777)   # distinct 範囲外＝必ず一意・かつ末尾
    return data


def measure_interleaved(base_fn, cand_fn, data, reps=31):
    """baseline / candidate を 1 rep ごとに交互計測（dedupe の bench と同形）。"""
    base_t, cand_t = [], []
    for i in range(reps):
        if i % 2 == 0:
            t0 = _perf_counter(); base_fn(data); base_t.append(_perf_counter() - t0)
            t0 = _perf_counter(); cand_fn(data); cand_t.append(_perf_counter() - t0)
        else:
            t0 = _perf_counter(); cand_fn(data); cand_t.append(_perf_counter() - t0)
            t0 = _perf_counter(); base_fn(data); base_t.append(_perf_counter() - t0)
    return base_t, cand_t
