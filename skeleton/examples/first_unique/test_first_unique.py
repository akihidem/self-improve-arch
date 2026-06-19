"""first_unique の正しさテスト（pytest）。sandbox が候補をこれにかけ pass/fail を採否入力にする。"""
from first_unique import first_unique


def test_basic():
    assert first_unique([1, 1, 2, 3, 3]) == 2


def test_none_when_all_repeat():
    assert first_unique([5, 5, 7, 7]) is None


def test_single():
    assert first_unique([7]) == 7


def test_empty():
    assert first_unique([]) is None


def test_strings():
    assert first_unique(["a", "b", "a", "c", "b"]) == "c"


def test_first_among_multiple_uniques():
    # 4,2 は重複・9,1 は一意。最初の一意は 9（最後の一意 1 を返す実装はここで落ちる）。
    assert first_unique([4, 2, 2, 4, 9, 1]) == 9
