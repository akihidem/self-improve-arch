"""rinne(生成) → self-improve-arch(厳密採否+昇格) を1本に繋ぐブリッジ＝正系の連結組織。

rinne エンジンは 1 タスクにつき implFile（`<module>.py` 全文）を生成する。それは
self-improve-arch の候補規約（`<module>.py` 全文の差し替え）と**同形**なので、rinne の出力を
そのまま候補スレートに流せる。本ブリッジは rinne 出力群を候補 dir に整え、self-improve-arch の
1 サイクル（sandbox + Reviewer + Judge + gate + confirm）で採否し、採用を promote で baseline へ昇格する。

役割分担（正系の境界どおり [[project-rinne]] [[project-self-improve-platform]]）:
  rinne             = 候補を「正しく作る」（生成器・L0 + 異種床 + 反例裁定は rinne 側の責務）
  self-improve-arch = 候補を「厳密に採否し本番手前まで昇格」（実テスト + 実ベンチ + 多重比較 + confirm + Ring）
LLM は self-improve-arch 側では起動しない（採否は決定的）。rinne 生成は別ステップ＝claude-in-claude 回避。

使い方:
  # rinne が生成した implFile 群（候補）を self-improve-arch に通して採否＋昇格
  python bridge.py --target-dir DIR --module MOD --symbol FN --primary KPI \
                   --rinne-outputs cand_a.py cand_b.py --apply staging --kb-path /tmp/kb.sqlite
"""
from __future__ import annotations

import argparse
import shutil
import sys
from dataclasses import dataclass, replace
from pathlib import Path

from budget import ConfirmBudget
from builder import BuilderDir
from kb import KnowledgeBase
from loop import _CONFIRM_SEEDS, run_one_cycle
from promote import PROMOTE_MODES, promote_winner
from review import Judge, make_reviewers
from sandbox import Task, infer_baseline_params


def stage_candidates(rinne_outputs, dest_dir) -> Path:
    """rinne が生成した implFile 群を self-improve-arch の候補 dir に整える（候補名＝ファイル stem）。"""
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    for f in rinne_outputs:
        src = Path(f)
        if not src.exists():
            raise FileNotFoundError(f"rinne 出力が無い: {src}")
        shutil.copyfile(src, dest / src.name)
    return dest


@dataclass
class LineResult:
    """正系1本を通した結果（採否＋昇格）。"""

    adopted: bool
    winner: str | None
    promotion: object | None   # PromotionRecord | None


def run_line(task: Task, candidates_dir, *, apply_mode: str = "staging",
             kb: KnowledgeBase | None = None, cycle: int = 1) -> LineResult:
    """候補 dir を self-improve-arch の1サイクルに通し、採用を昇格する（正系の右半分）。"""
    if apply_mode not in PROMOTE_MODES:
        raise ValueError(f"unknown apply mode: {apply_mode!r}")
    kb = kb or KnowledgeBase(":memory:")
    if not task.baseline_params:
        task = replace(task, baseline_params=infer_baseline_params(task) or ("items",))
    reviewers = make_reviewers("mock", task.symbol, task.baseline_params)
    judge = Judge()
    cb = ConfirmBudget(kb, _CONFIRM_SEEDS, 3)
    builder = BuilderDir(str(candidates_dir))

    out = run_one_cycle(builder, reviewers, judge, kb, cycle, None, cb, task)
    prom = None
    if out.adopted and out.winner_source is not None:
        prom = promote_winner(
            task, out.winner, out.winner_source, mode=apply_mode,
            primary_rel=out.confirm_detail.get(task.primary, {}).get("rel", 0.0),
            confirm_seed=out.confirm_seed, kb=kb,
        )
    return LineResult(adopted=out.adopted, winner=out.winner, promotion=prom)


def main() -> int:
    ap = argparse.ArgumentParser(description="rinne 出力を self-improve-arch に通して採否+昇格する正系ブリッジ")
    ap.add_argument("--target-dir", required=True)
    ap.add_argument("--module", required=True)
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--primary", default="latency")
    ap.add_argument("--higher-is-better", action="store_true")
    ap.add_argument("--rinne-outputs", nargs="+", required=True, help="rinne が生成した候補 implFile 群")
    ap.add_argument("--staging-dir", default=None, help="候補を整える dir（既定: <target-dir>/_rinne_candidates）")
    ap.add_argument("--apply", choices=list(PROMOTE_MODES), default="staging")
    ap.add_argument("--kb-path", default=":memory:")
    a = ap.parse_args()

    task = Task(target_dir=Path(a.target_dir), module=a.module, symbol=a.symbol,
                primary=a.primary, higher_is_better=a.higher_is_better)
    staging = a.staging_dir or str(Path(a.target_dir) / "_rinne_candidates")
    cands = stage_candidates(a.rinne_outputs, staging)
    kb = KnowledgeBase(a.kb_path)
    res = run_line(task, cands, apply_mode=a.apply, kb=kb)

    print(f"rinne→self-improve-arch: candidates={len(a.rinne_outputs)} target={a.module}:{a.symbol}")
    if res.adopted:
        print(f"  => 採用: {res.winner}")
        if res.promotion and res.promotion.applied:
            print(f"  => 昇格({res.promotion.mode}): {res.promotion.path}")
    else:
        print("  => 不採用（厳密ゲートを通過した候補なし）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
