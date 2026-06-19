"""dedupe_preserve_order の実ベンチ（latency, lower better）。

多重複の大リスト（5000 要素・500 種）で計測する。重複が多いほど
O(n^2) baseline と O(n) seen-set 候補の差が大きく出る。

measure(reps) は各 rep の実行時間（秒）の list を返す。sandbox 側で
baseline と candidate を同じ workload に通し、mean/std/n を取る。
"""
from __future__ import annotations

import time

# 計測クロックを module ロード時に束縛退避する。候補は後から time.perf_counter を
# setattr で差し替えうるが、束縛済み _perf_counter（元の関数オブジェクト）には効かない。
# 防御 in depth であって境界ではない（sandbox.py の「信頼境界」参照）。
_perf_counter = time.perf_counter

from dedupe import dedupe_preserve_order


def make_workload(size=5000, distinct=500, seed=1234):
    # 決定的（seed 固定）な多重複リスト。size > distinct なので必ず重複が出る。
    # seed を変えると探索に未使用の fresh slice（confirm 用）になる。
    import random

    rng = random.Random(seed)
    return [rng.randrange(distinct) for _ in range(size)]


def measure(reps=5, size=5000, distinct=500):
    """dedupe を reps 回実行し、各回の経過秒を返す。"""
    data = make_workload(size, distinct)
    timings = []
    for _ in range(reps):
        t0 = _perf_counter()
        dedupe_preserve_order(data)
        timings.append(_perf_counter() - t0)
    return timings


def measure_interleaved(base_fn, cand_fn, data, reps=31):
    """baseline / candidate を同一プロセス内で 1 rep ごとに交互計測する。

    clock は束縛退避した _perf_counter を使い（候補の module 属性差し替えに耐える。
    ただし境界ではない・sandbox.py 参照）、候補関数は「呼ばれるだけ」。プロセスを
    分けず交互に呼ぶことで、warmup・スケジューリング等の系統差が双方に等しく乗り相殺される。
    返り値 (base_timings, cand_timings) はそれぞれ長さ reps の秒 list。
    """
    base_t, cand_t = [], []
    # 1 rep ずつ baseline→candidate と交互に。順序由来の偏りを避けるため
    # rep が奇数のときは candidate→baseline と入れ替える。
    for i in range(reps):
        if i % 2 == 0:
            t0 = _perf_counter(); base_fn(data); base_t.append(_perf_counter() - t0)
            t0 = _perf_counter(); cand_fn(data); cand_t.append(_perf_counter() - t0)
        else:
            t0 = _perf_counter(); cand_fn(data); cand_t.append(_perf_counter() - t0)
            t0 = _perf_counter(); base_fn(data); base_t.append(_perf_counter() - t0)
    return base_t, cand_t


if __name__ == "__main__":
    import statistics

    ts = measure()
    print(f"reps={len(ts)} mean={statistics.mean(ts)*1000:.3f}ms "
          f"std={statistics.pstdev(ts)*1000:.3f}ms")
