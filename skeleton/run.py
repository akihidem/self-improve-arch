#!/usr/bin/env python3
"""CLI: self-improvement ループを N サイクル回す（任意ターゲット・実候補対応）。

  # 同梱 dedupe を mock 候補でデモ
  python run.py --builder mock --reviewers mock --cycles 3

  # 自分のターゲット × 自分の候補ファイル群を full rigor で採否（実運用）
  python run.py --target-dir DIR --module MOD --symbol FN --primary KPI \
                --candidates-dir CANDS --cycles 1 --kb-path /tmp/kb.sqlite

  # 数値コード（numpy/scipy 依存・venv）の例: 依存解決(--python)と import 許可(--allow-imports)
  V=path/to/.venv/bin/python
  $V run.py --target-dir DIR --module MOD --symbol FN --python $V \
            --allow-imports numpy,scipy --candidates-dir CANDS --cycles 1 --kb-path /tmp/kb.sqlite

--baseline-params は省略すると baseline のシグネチャから自動推論する（手で渡す必要は通常無い）。

1 サイクルで候補 slate を生成し、各候補を sandbox+Reviewer+Judge+gate で評価する。gate は
Bonferroni（alpha/K）で多重比較補正し、valid な最良を選び、探索に未使用の fresh confirm slice で
再確証できたものだけ採用する。confirm holdout には query-budget（slice あたり B 回・KB 永続）を課し、
全 slice 枯渇したら採用を止める。各候補の採否・実測値・Reviewer blocking・confirm 結果・winner を出力。

ターゲット規約: --target-dir に `<module>.py`（<symbol> を定義）/ `test_<module>.py`（pytest）/
`bench.py`（make_workload(seed) と measure_interleaved(base_fn, cand_fn, data, reps) を <symbol>
前提で提供）。候補は `<module>.py` 全文を差し替えるソース。

候補の出どころ: --candidates-dir のファイル群（BuilderDir）。手書き / 別途 LLM 出力 / 過去案いずれも可。
mock/cli-run は同梱デモ用。cli-run（claude-cli-run）は wiring のみで自動実行では選ばない（claude-in-claude 回避）。

**安全境界（重要）**: 候補は untrusted コードとして実行される。AST 検査はセキュリティ境界ではない
（sandbox.py 参照）。実行は `--isolation` の OS 隔離下で行う（既定 rlimit=非特権の DoS 床）。**真に
信頼できない候補の network exfiltration まで止めるのは `--isolation docker`（--network none）だけ**で、
rlimit/systemd は資源上限のみ（network 非隔離）。docker 不可の環境では OS/コンテナ分離を外側で用意すること。
`--allow-imports` は候補に追加 import を許す＝候補を更に信頼する操作なので、信頼できる候補にのみ使うこと。
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from pathlib import Path

from budget import ConfirmBudget
from builder import BuilderDir, make_builder
from isolation import detect_backend, isolation_note
from kb import KnowledgeBase
from loop import _CONFIRM_SEEDS, run_one_cycle
from promote import PROMOTE_MODES, promote_winner
from review import Judge, make_reviewers
from sandbox import DEDUPE_TASK, Task, infer_baseline_params

DEFAULT_KB = str(Path(__file__).resolve().parent / "kb.sqlite")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="self-improvement loop (gate + reviewers + judge + diversity + confirm + budget)")
    ap.add_argument("--builder", choices=["mock", "cli-run"], default="mock")
    ap.add_argument("--reviewers", choices=["mock", "cli-run"], default="mock")
    ap.add_argument("--candidates-dir", default=None,
                    help="実候補ファイル(<module>.py 群)のディレクトリ。指定すると builder より優先（BuilderDir）")
    ap.add_argument("--target-dir", default=None,
                    help="改善対象 dir（<module>.py / test_<module>.py / bench.py）。省略時は同梱 dedupe")
    ap.add_argument("--module", default="dedupe")
    ap.add_argument("--symbol", default="dedupe_preserve_order")
    ap.add_argument("--primary", default="latency", help="主要 KPI 名（bench は lower-better サンプル列を返す前提）")
    ap.add_argument("--higher-is-better", action="store_true", help="主要 KPI が高いほど良い場合")
    ap.add_argument("--baseline-params", default=None,
                    help="baseline 関数の引数名（カンマ区切り）。省略時は baseline シグネチャから自動推論")
    ap.add_argument("--python", default=None,
                    help="テスト/ベンチ subprocess の python（プロジェクトの venv 指定）。省略時は実行中の python")
    ap.add_argument("--allow-imports", default=None,
                    help="候補に許可する import の top-module（カンマ区切り。例 numpy,scipy）。信頼候補のみ")
    ap.add_argument("--isolation", choices=["auto", "docker", "systemd", "rlimit", "none"],
                    default="rlimit",
                    help="候補実行の OS 隔離 backend。auto=最強自動 / docker=真の OS+network 境界 / "
                         "rlimit=非特権 DoS 床（既定）/ systemd=cgroup / none=隔離なし（信頼候補のみ）")
    ap.add_argument("--mem-mb", type=int, default=1024, help="隔離時のメモリ上限（MB）")
    ap.add_argument("--cpu-s", type=int, default=120, help="隔離時の CPU 秒上限")
    ap.add_argument("--reps", type=int, default=31)
    ap.add_argument("--slate-size", type=int, default=0, help="0=全候補（BuilderDir）。mock は固定 slate")
    ap.add_argument("--temperature", type=float, default=0.7)
    ap.add_argument("--confirm-budget", type=int, default=3,
                    help="confirm holdout 1 slice あたりの query 上限（KB 永続）")
    ap.add_argument("--cycles", type=int, default=3, help="実候補一括採否なら 1 を推奨")
    ap.add_argument("--kb-path", default=DEFAULT_KB)
    ap.add_argument("--apply", choices=list(PROMOTE_MODES), default="staging",
                    help="採用された勝者の昇格先。staging=<module>.promoted.py に提案（既定・prod 直書きしない）"
                         " / baseline=live baseline を上書き（退避つき・隔離環境のみ）/ none=決定のみ")
    a = ap.parse_args()

    # task: --target-dir を渡せば任意ターゲット。省略時は同梱 dedupe（後方互換）。
    if a.target_dir:
        task = Task(target_dir=Path(a.target_dir), module=a.module, symbol=a.symbol,
                    primary=a.primary, higher_is_better=a.higher_is_better, reps=a.reps)
    else:
        task = DEDUPE_TASK
    # CLI 由来の実行設定を注入（python / 許可 import）。
    allowed = ("__future__",) + tuple(m.strip() for m in (a.allow_imports or "").split(",") if m.strip())
    task = replace(task, python_exe=(a.python or ""), allowed_imports=allowed,
                   isolation=a.isolation, mem_mb=a.mem_mb, cpu_s=a.cpu_s)
    # baseline_params: 明示 > baseline シグネチャからの自動推論 > 既定。
    if a.baseline_params:
        bparams = tuple(p.strip() for p in a.baseline_params.split(",") if p.strip())
    else:
        bparams = infer_baseline_params(task) or task.baseline_params
    task = replace(task, baseline_params=bparams)

    # builder: --candidates-dir があれば実候補(BuilderDir)、無ければ mock/cli-run（デモ）。
    if a.candidates_dir:
        builder = BuilderDir(a.candidates_dir)
        builder_label = f"dir:{a.candidates_dir}"
    else:
        builder = make_builder(a.builder, temperature=a.temperature)
        builder_label = a.builder

    reviewers = make_reviewers(a.reviewers, task.symbol, task.baseline_params)
    judge = Judge()
    kb = KnowledgeBase(a.kb_path)
    confirm_budget = ConfirmBudget(kb, _CONFIRM_SEEDS, a.confirm_budget)
    slate_size = None if a.slate_size == 0 else a.slate_size

    print(f"builder={builder_label} reviewers={a.reviewers} "
          f"target={task.module}:{task.symbol} primary={task.primary} "
          f"cycles={a.cycles} confirm_budget={a.confirm_budget}/slice kb={a.kb_path}")
    _iso = detect_backend() if task.isolation == "auto" else task.isolation
    print(f"隔離: 指定={task.isolation} 実効={_iso}（mem={task.mem_mb}MB cpu={task.cpu_s}s）— {isolation_note(_iso)}")
    print(f"証明: 採否は実テスト + 実ベンチ ({task.primary} の有意差) が床。Reviewer/Judge は必要条件。"
          f"複数提案は Bonferroni 補正し最良を選び fresh confirm slice で再確証。query-budget 枯渇で停止。\n")

    adopted_count = 0
    for cycle in range(1, a.cycles + 1):
        out = run_one_cycle(builder, reviewers, judge, kb, cycle, slate_size,
                            confirm_budget, task)
        print(f"[cycle {out.cycle}] slate={out.slate_size}候補  "
              f"search alpha: 0.05 -> {out.alpha_corrected:.4g} (Bonferroni /{out.slate_size})")
        for r in out.results:
            d = r.detail.get(task.primary, {})
            mark = "★" if r.name == out.winner else " "
            print(f"  {mark} {r.name:16s} {r.status:12s} "
                  f"tests={r.tests_passed} rel={d.get('rel')} p={d.get('p')} "
                  f"significant={d.get('significant')}")
            for rv in r.reviews:
                if rv.blocking:
                    print(f"        review[{rv.role}] BLOCK :: {'; '.join(rv.blocking)}")
            if not r.adopt:
                print(f"        理由: {', '.join(r.reasons)}")
        # confirm: winner を query-budget 内の fresh slice で再確証（枯渇なら confirm 不可）
        if out.winner is not None:
            if out.exhausted:
                print(f"  confirm[{out.winner}]: query-budget 枯渇 → confirm 不可（要 fresh data）")
            else:
                cd = out.confirm_detail.get(task.primary, {})
                print(f"  confirm[{out.winner}] slice seed={out.confirm_seed}: "
                      f"rel={cd.get('rel')} p={cd.get('p')} "
                      f"significant={cd.get('significant')} -> confirmed={out.confirmed}")
            if not out.confirmed:
                print(f"        理由: {', '.join(out.confirm_reasons)}")
        # 最終採否（= search 最良選択 AND confirm 再現）
        if out.adopted:
            print(f"  => 採用: {out.winner}（search 最良 → confirm 再現）")
            adopted_count += 1
            # ループを閉じる: 採用された勝者を baseline へ昇格（Ring 規律は --apply で切替）。
            if a.apply != "none" and out.winner_source is not None:
                pr = promote_winner(task, out.winner, out.winner_source, mode=a.apply,
                                    primary_rel=out.confirm_detail.get(task.primary, {}).get("rel", 0.0),
                                    confirm_seed=out.confirm_seed, kb=kb)
                if pr.mode == "staging":
                    print(f"  => 昇格(staging): 提案を {pr.path} に書込（live baseline 不変・prod適用は人間/CI）")
                elif pr.mode == "baseline":
                    print(f"  => 昇格(baseline): {pr.from_sha or '∅'}→{pr.to_sha} に差替（退避 {pr.backup}）")
        elif out.exhausted:
            print(f"  => 不採用（{out.winner} は holdout 枯渇で確証できず）")
        elif out.winner is not None:
            print(f"  => 不採用（{out.winner} は search 通過も confirm で再現せず）")
        else:
            print("  => 不採用（valid な候補なし）")
        print()

    print(f"=== {adopted_count}/{a.cycles} サイクルで採用 ===")
    print("confirm holdout 予算（KB 永続）:")
    for h in confirm_budget.status():
        flag = " (枯渇)" if h["exhausted"] else ""
        print(f"   seed={h['seed']} {h['spent']}/{h['budget']}{flag}")
    print("KB 最新:")
    for row in kb.recent(limit=20):
        print("  ", row)
    proms = kb.promotions(limit=10)
    if proms:
        print("昇格履歴（ループを閉じた記録）:")
        for p in proms:
            print(f"   [{p['mode']}] {p['module']}: {p['from_sha'] or '∅'}→{p['to_sha']} "
                  f"rel={p['primary_rel']} seed={p['confirm_seed']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
