"""正系ブリッジ（bridge.py）の回帰 ＝ rinne 出力が self-improve-arch を通って採用+昇格すること。

rinne 生成は LLM なのでテストでは「rinne 出力の stand-in」＝既存の候補ファイルを使い、
ブリッジの右半分（候補整形 → 厳密採否 → 昇格）が決定的に通ることを検証する。
"""
import shutil
from pathlib import Path

from bridge import run_line, stage_candidates
from kb import KnowledgeBase
from sandbox import Task

_EX = Path(__file__).resolve().parent / "examples" / "first_unique"
_CANDS = Path(__file__).resolve().parent / "examples" / "first_unique_candidates"


def test_stage_candidates_copies_outputs(tmp_path):
    dest = stage_candidates([_CANDS / "correct_fast.py", _CANDS / "wrong.py"], tmp_path / "c")
    assert (dest / "correct_fast.py").exists() and (dest / "wrong.py").exists()


def test_stage_missing_output_raises(tmp_path):
    import pytest
    with pytest.raises(FileNotFoundError):
        stage_candidates([tmp_path / "nope.py"], tmp_path / "c")


def test_line_adopts_and_promotes_staging(tmp_path):
    # rinne 出力(stand-in): 速い正解と誤実装。target は first_unique のコピー。
    tgt = tmp_path / "first_unique"
    shutil.copytree(_EX, tgt)
    baseline_before = (tgt / "first_unique.py").read_text()
    task = Task(target_dir=tgt, module="first_unique", symbol="first_unique", primary="latency")
    cands = stage_candidates([_CANDS / "correct_fast.py", _CANDS / "wrong.py"], tmp_path / "c")
    kb = KnowledgeBase(str(tmp_path / "kb.sqlite"))

    res = run_line(task, cands, apply_mode="staging", kb=kb)

    assert res.adopted and res.winner == "correct_fast"
    # staging は live baseline を触らず提案ファイルを出す（Ring-1 安全）。
    assert (tgt / "first_unique.py").read_text() == baseline_before
    assert (tgt / "first_unique.promoted.py").exists()
    assert res.promotion.mode == "staging" and res.promotion.applied
    assert kb.promotions()[0]["to_sha"] == res.promotion.to_sha


def test_line_no_valid_candidate(tmp_path):
    # 誤実装のみ → 厳密ゲートで不採用・昇格なし。
    tgt = tmp_path / "first_unique"
    shutil.copytree(_EX, tgt)
    task = Task(target_dir=tgt, module="first_unique", symbol="first_unique", primary="latency")
    cands = stage_candidates([_CANDS / "wrong.py"], tmp_path / "c")
    res = run_line(task, cands, apply_mode="staging", kb=KnowledgeBase(":memory:"))
    assert res.adopted is False and res.promotion is None
