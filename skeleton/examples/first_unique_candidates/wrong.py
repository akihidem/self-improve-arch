"""候補: 速いが不正（最初でなく最後の一意を返す＝順序契約違反）。テストで落ちる。"""
from __future__ import annotations


def first_unique(items):
    counts = {}
    for x in items:
        counts[x] = counts.get(x, 0) + 1
    last = None
    for x in items:
        if counts[x] == 1:
            last = x
    return last
