"""昇格段（promote.py）の回帰 ＝ self-improvement ループが閉じることの検証。

unit: staging は live baseline を触らず提案ファイルを書く / baseline は退避つきで可逆に上書き /
      none は何も書かない・KB 記録なし。
e2e:  cycle1 で correct_fast を採用 → baseline へ昇格 → cycle2 は baseline が前進した結果
      もう採用が出ない（＝ループが閉じ、改善後 baseline の上で再評価されている）。
"""
import shutil
from pathlib import Path

from budget import ConfirmBudget
from builder import BuilderDir
from kb import KnowledgeBase
from loop import _CONFIRM_SEEDS, run_one_cycle
from promote import promote_winner
from review import Judge, make_reviewers
from sandbox import Task, infer_baseline_params

_EX = Path(__file__).resolve().parent / "examples" / "first_unique"
_CANDS = Path(__file__).resolve().parent / "examples" / "first_unique_candidates"

_BASE = "def f(items):\n    return list(items)\n"
_WIN = "def f(items):\n    return items[:1]\n"


def _task(tmp_path):
    (tmp_path / "m.py").write_text(_BASE, encoding="utf-8")
    return Task(target_dir=tmp_path, module="m", symbol="f", primary="latency")


# --- unit: 3 モード ----------------------------------------------------------

def test_staging_writes_proposal_not_baseline(tmp_path):
    task = _task(tmp_path)
    kb = KnowledgeBase(":memory:")
    rec = promote_winner(task, "win", _WIN, mode="staging", primary_rel=-0.5,
                         confirm_seed=99991, kb=kb)
    assert rec.applied and rec.mode == "staging"
    # live baseline は不変、提案だけ別ファイルに出る（Ring-1 安全）。
    assert (tmp_path / "m.py").read_text() == _BASE
    assert (tmp_path / "m.promoted.py").read_text() == _WIN
    proms = kb.promotions()
    assert len(proms) == 1 and proms[0]["mode"] == "staging" and proms[0]["to_sha"] == rec.to_sha


def test_baseline_overwrites_with_backup_reversible(tmp_path):
    task = _task(tmp_path)
    kb = KnowledgeBase(":memory:")
    rec = promote_winner(task, "win", _WIN, mode="baseline", primary_rel=-0.5, kb=kb)
    assert rec.applied and rec.mode == "baseline"
    # baseline は勝者に差し替わる。退避ファイルに直前が残り、戻せる（可逆）。
    assert (tmp_path / "m.py").read_text() == _WIN
    bak = Path(rec.backup)
    assert bak.exists() and bak.read_text() == _BASE
    shutil.copyfile(bak, tmp_path / "m.py")            # ロールバック
    assert (tmp_path / "m.py").read_text() == _BASE
    assert kb.promotions()[0]["mode"] == "baseline"


def test_none_does_not_write_and_not_recorded(tmp_path):
    task = _task(tmp_path)
    kb = KnowledgeBase(":memory:")
    rec = promote_winner(task, "win", _WIN, mode="none", kb=kb)
    assert not rec.applied and rec.path == ""
    assert (tmp_path / "m.py").read_text() == _BASE
    assert not (tmp_path / "m.promoted.py").exists()
    assert kb.promotions() == []


def test_unknown_mode_raises(tmp_path):
    import pytest
    with pytest.raises(ValueError):
        promote_winner(_task(tmp_path), "w", _WIN, mode="prod")


# --- e2e: ループが閉じる（baseline が前進する）-------------------------------

def test_loop_closes_baseline_advances(tmp_path):
    # examples/first_unique を tmp にコピー（promote が baseline を書き換えるため）。
    tgt = tmp_path / "first_unique"
    shutil.copytree(_EX, tgt)
    task = Task(target_dir=tgt, module="first_unique", symbol="first_unique", primary="latency")
    task = task.__class__(**{**task.__dict__,
                            "baseline_params": infer_baseline_params(task) or ("items",)})
    reviewers = make_reviewers("mock", task.symbol, task.baseline_params)
    judge = Judge()
    kb = KnowledgeBase(str(tmp_path / "kb.sqlite"))
    cb = ConfirmBudget(kb, _CONFIRM_SEEDS, 3)
    builder = BuilderDir(str(_CANDS))

    # cycle 1: correct_fast が遅い baseline を有意に上回り採用される。
    out1 = run_one_cycle(builder, reviewers, judge, kb, 1, None, cb, task)
    assert out1.adopted and out1.winner == "correct_fast"
    assert out1.winner_source is not None

    # 昇格でループを閉じる: 勝者を baseline へ（＝次サイクルの比較基準が前進）。
    pr = promote_winner(task, out1.winner, out1.winner_source, mode="baseline",
                        confirm_seed=out1.confirm_seed, kb=kb)
    assert (tgt / "first_unique.py").read_text() == out1.winner_source  # baseline が勝者に
    assert Path(pr.backup).exists()                                      # 旧 baseline は退避済

    # cycle 2: baseline は既に correct_fast。同じ候補を当てても改善余地が無く採用が出ない
    #          ＝ループが閉じ、改善後 baseline の上で再評価されている証拠。
    out2 = run_one_cycle(builder, reviewers, judge, kb, 2, None, cb, task)
    assert out2.adopted is False
