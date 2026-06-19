"""dedupe_preserve_order の正しさテスト（pytest 形式）。

このテストが「実テスト結果」のソース。sandbox は候補をこのテストに
かけ、pass/fail を採否ゲートの入力にする（候補の自己申告ではない）。
"""
from dedupe import dedupe_preserve_order


def test_removes_duplicates():
    assert dedupe_preserve_order([1, 1, 2, 2, 3]) == [1, 2, 3]


def test_preserves_first_occurrence_order():
    # 順序保持が要件の核。list(set(...)) のような順序を壊す候補はここで落ちる。
    assert dedupe_preserve_order([3, 1, 2, 1, 3, 2]) == [3, 1, 2]


def test_empty():
    assert dedupe_preserve_order([]) == []


def test_all_duplicates():
    assert dedupe_preserve_order([7, 7, 7, 7]) == [7]


def test_no_duplicates():
    assert dedupe_preserve_order([1, 2, 3, 4]) == [1, 2, 3, 4]


def test_strings_hashable():
    assert dedupe_preserve_order(["a", "b", "a", "c", "b"]) == ["a", "b", "c"]


def test_does_not_mutate_input():
    src = [1, 2, 2, 3]
    dedupe_preserve_order(src)
    assert src == [1, 2, 2, 3]
