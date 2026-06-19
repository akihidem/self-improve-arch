"""候補: baseline と同じ O(n^2)。正しいが速くならない（主要KPI 未達で不採用）。"""
from __future__ import annotations


def first_unique(items):
    for x in items:
        if items.count(x) == 1:
            return x
    return None
