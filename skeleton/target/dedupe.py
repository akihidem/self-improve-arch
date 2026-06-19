"""改善対象（toy だが本物の関数）。

dedupe_preserve_order(items): 出現順を保ったまま重複を除去して返す。

これは「遅い baseline」実装: `x not in result` が毎回 result 全体を線形走査
するため計算量は O(n^2)。多重複の大リストで顕著に遅い。
self-improvement ループは、これより速くて正しい候補を提案・検証・採否する。
"""
from __future__ import annotations


def dedupe_preserve_order(items):
    """出現順を保って重複除去（O(n^2) baseline 実装）。

    前提: items の要素は hashable / 比較可能。
    """
    result = []
    for x in items:
        if x not in result:
            result.append(x)
    return result
