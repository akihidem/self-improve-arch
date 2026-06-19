"""改善対象（例2・dedupe とは別関数で汎用性を示す）。

first_unique(items): 最初に「1 回だけ現れる」要素を返す（無ければ None）。
baseline は各要素ごとに items.count(x) を呼ぶ O(n^2) 実装で、一意要素が遅く見つかる
大リストで顕著に遅い。self-improvement ループはこれより速くて正しい候補を採否する。
"""
from __future__ import annotations


def first_unique(items):
    """最初の一意要素を返す（O(n^2) baseline: items.count を毎回呼ぶ）。

    前提: 要素は hashable / 比較可能。
    """
    for x in items:
        if items.count(x) == 1:
            return x
    return None
