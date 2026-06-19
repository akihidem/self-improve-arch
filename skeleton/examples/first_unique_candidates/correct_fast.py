"""候補: count 辞書で O(n)。正しく速い（採用されるはず）。"""
from __future__ import annotations


def first_unique(items):
    counts = {}
    for x in items:
        counts[x] = counts.get(x, 0) + 1
    for x in items:
        if counts[x] == 1:
            return x
    return None
