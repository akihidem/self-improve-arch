"""Reviewer 2体 + Judge — 採否の床に「必要条件」を足す層。

この層は採否を **veto することしかできない**（DESIGN.md §0 の不変条件）。
ゲート不合格の候補を救済できず、KPI 生値にも触れない。実装上は、各レビュアーが
候補コードを検査して approve/blocking を出し、Judge がそれを **決定的ポリシー**で
集約して `judge_approved: bool` を返すだけ。この bool は gate.evaluate_gates が
tests/KPI/guardrail と **AND** する一入力に過ぎず、True でも tests が落ちれば
不採用・False なら強制不採用。よって「床は動かない・必要条件が増えるだけ」。

honest scope（DESIGN-SUPPLEMENT-v2 の天井と整合）:
  ReviewerMock は **構造的・コード可視**の欠陥（mutable default 引数・契約外の
  シグネチャ拡大など、AST で決定的に見える性質）だけを検出する。**subtle/R2
  （overfit・subtle poison・未観測次元のドリフト・construct drift）は捕れない**し、
  捕れると主張しない。内部レビューの天井の内側＝R1 の必要条件を足すのが役目。
  実 LLM レビュー（ReviewerCliRun）は粒度を上げるが、それでも R2 の床にはならない。

claude-in-claude 回避:
  ReviewerCliRun は builder.BuilderCliRun と同じく **wiring のみ**。この workflow の
  自動実行では選ばない（`make_reviewers("mock")` を使う）。`claude -p` は使わず
  claude-cli-run（対話TUI=サブスク枠）を呼ぶ設計。
"""
from __future__ import annotations

import ast
import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

# baseline 契約: dedupe_preserve_order(items)。公開シグネチャはこの 1 引数のみ。
_BASELINE_PARAMS = frozenset({"items"})
# mutable な既定値を生む構築子（引数 default に現れたら共有可変状態の疑い）。
_MUTABLE_CTORS = frozenset({
    "set", "list", "dict", "bytearray", "defaultdict", "deque",
    "Counter", "OrderedDict",
})


@dataclass
class Review:
    """1 レビュアーの所見。blocking が空でなければ veto を主張する。"""

    reviewer: str          # 表示名（例 "reviewer-A"）
    role: str              # "safety" / "scope"
    approve: bool
    blocking: list = field(default_factory=list)   # 採用を止める指摘
    notes: list = field(default_factory=list)       # 非 blocking の所見


@dataclass
class JudgeVerdict:
    """Judge が複数 Review を決定的に集約した結果。"""

    approved: bool
    reason: str
    reviews: list = field(default_factory=list)


# --- AST ヘルパ（決定的・コード可視の性質だけを見る）-------------------------

def _all_arg_names(fn: ast.FunctionDef) -> list:
    a = fn.args
    args = list(a.posonlyargs) + list(a.args) + list(a.kwonlyargs)
    names = [x.arg for x in args]
    if a.vararg:
        names.append(a.vararg.arg)
    if a.kwarg:
        names.append(a.kwarg.arg)
    return names


def _has_mutable_default(fn: ast.FunctionDef) -> bool:
    """引数の既定値に mutable リテラル/構築子があるか（共有可変状態の典型）。"""
    defaults = list(fn.args.defaults) + [d for d in fn.args.kw_defaults if d is not None]
    for d in defaults:
        if isinstance(d, (ast.List, ast.Dict, ast.Set)):
            return True
        if isinstance(d, ast.Call) and isinstance(d.func, ast.Name) \
                and d.func.id in _MUTABLE_CTORS:
            return True
    return False


def _functions(source: str) -> list:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    return [n for n in tree.body if isinstance(n, ast.FunctionDef)]


# --- Reviewer -----------------------------------------------------------------

class ReviewerMock:
    """決定的な構造検査レビュアー（LLM 非依存）。role で観点を変える。

    safety: 引数の mutable default（非 reentrant な共有可変状態）を blocking。
    scope : 公開関数 symbol が baseline 契約（baseline_params）を超える引数を増やしたら
            blocking、補助 top-level 関数の追加は note。
    どちらも候補ソース（コード可視の性質）だけで判断し、KPI 生値は使わない。
    symbol / baseline_params で任意ターゲットに対応（既定は同梱 dedupe）。
    """

    def __init__(self, role: str, symbol: str = "dedupe_preserve_order",
                 baseline_params=("items",)) -> None:
        if role not in ("safety", "scope"):
            raise ValueError(f"unknown reviewer role: {role!r}")
        self.role = role
        self.reviewer = f"reviewer-{role}"
        self.symbol = symbol
        self.baseline_params = frozenset(baseline_params)

    def review(self, candidate_name: str, source: str, sb=None) -> Review:
        funcs = _functions(source)
        target = next((f for f in funcs if f.name == self.symbol), None)
        blocking: list = []
        notes: list = []

        if target is None:
            # ここに来る候補は AST vet 済みのはず（保険）。
            blocking.append(f"{self.symbol} が見つからない")
        elif self.role == "safety":
            if _has_mutable_default(target):
                blocking.append(
                    "引数に共有 mutable default を持つ（def 時生成・全呼び出しで共有 "
                    "= 非 reentrant / 並行呼び出しで状態破壊）"
                )
        elif self.role == "scope":
            extra = [p for p in _all_arg_names(target) if p not in self.baseline_params]
            if extra:
                blocking.append(
                    f"公開シグネチャを契約外に拡大: 追加引数 {extra}"
                    f"（baseline は {sorted(self.baseline_params)} のみ）"
                )
            extra_funcs = [f.name for f in funcs if f.name != self.symbol]
            if extra_funcs:
                notes.append(f"補助関数を追加: {extra_funcs}")

        return Review(reviewer=self.reviewer, role=self.role,
                      approve=len(blocking) == 0, blocking=blocking, notes=notes)


class ReviewerCliRun:
    """claude-cli-run 経由の実 LLM レビュアー（wiring のみ。workflow では実走しない）。

    builder.BuilderCliRun と同じ規約: claude-cli-run（対話TUI=サブスク枠）を呼び、
    `claude -p`（Agent SDK クレジット枠）は使わない。不在/失敗は明示 raise し、
    黙って mock に fallback しない。claude-in-claude 回避のため自動実行では選ばない。
    """

    def __init__(self, role: str, symbol: str = "dedupe_preserve_order",
                 script_path: str | None = None,
                 model: str | None = None, timeout: int = 300) -> None:
        if role not in ("safety", "scope"):
            raise ValueError(f"unknown reviewer role: {role!r}")
        self.role = role
        self.reviewer = f"reviewer-{role}"
        self.symbol = symbol
        self.script_path = script_path or str(
            Path.home() / ".claude" / "scripts" / "claude-cli-run.py"
        )
        self.model = model
        self.timeout = timeout

    _ROLE_FOCUS = {
        "safety": "正しさ・安全性（副作用・非 reentrant な共有状態・テスト範囲外の"
                  "前提・例外処理）。テストが緑でもコード上危険なら blocking にする。",
        "scope": "スコープと保守性（baseline 契約 (items) を超える公開シグネチャ拡大・"
                 "不要な複雑さ・余計な追加要素）。",
    }

    def _build_prompt(self, source: str) -> str:
        return (
            f"あなたはコードレビュアーです。観点: {self._ROLE_FOCUS[self.role]}\n"
            f"次の {self.symbol} 候補を、この観点だけからレビューしてください。\n"
            "採否そのものは別の決定的ゲートが決めます。あなたは観点上の blocking 指摘"
            "（採用を止めるべき問題）と note を出すだけです。\n"
            "回答は厳密に次の JSON のみ（コードフェンス無し）:\n"
            '{"approve": <bool>, "blocking": [<string>...], "notes": [<string>...]}\n\n'
            f"--- 候補ソース ---\n{source}"
        )

    def review(self, candidate_name: str, source: str, sb=None) -> Review:
        if not os.path.exists(self.script_path):
            raise FileNotFoundError(
                f"claude-cli-run が見つからない: {self.script_path}（mock に fallback しない）"
            )
        cmd = ["python3", self.script_path, "--permission-mode", "plan",
               "--no-sentinel", self._build_prompt(source)]
        if self.model:
            cmd[3:3] = ["--model", self.model]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=self.timeout)
        if proc.returncode != 0:
            raise RuntimeError(
                f"claude-cli-run 失敗 (exit={proc.returncode}): {proc.stderr.strip()[:300]}"
            )
        return self._parse(proc.stdout)

    def _parse(self, stdout: str) -> Review:
        text = stdout.strip()
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise ValueError(f"レビュー JSON を抽出できない: {text[:200]!r}")
        d = json.loads(text[start:end + 1])
        blocking = [str(x) for x in d.get("blocking", [])]
        return Review(
            reviewer=self.reviewer, role=self.role,
            approve=bool(d.get("approve", len(blocking) == 0)),
            blocking=blocking, notes=[str(x) for x in d.get("notes", [])],
        )


# --- Judge --------------------------------------------------------------------

class Judge:
    """決定的な veto 合議。reviewer の blocking が 1 つでもあれば不承認。

    これは「全員 approve が必要条件」= veto 専用ポリシー。Judge は KPI 生値を
    見ず、reviewer の出力（助言入力）を **コードで定義した結合規則**で集約するだけ。
    LLM 仲裁ではなく決定的＝監査可能。返す approved は gate への一入力に過ぎない。
    """

    def decide(self, reviews: list) -> JudgeVerdict:
        flagged = [(r.role, b) for r in reviews for b in r.blocking]
        if not flagged:
            return JudgeVerdict(
                approved=True,
                reason=f"全レビュアー承認（blocking なし・{len(reviews)}名）",
                reviews=reviews,
            )
        detail = "; ".join(f"[{role}] {b}" for role, b in flagged)
        return JudgeVerdict(approved=False, reason=f"Judge 未承認: {detail}",
                            reviews=reviews)


def make_reviewers(kind: str, symbol: str = "dedupe_preserve_order",
                   baseline_params=("items",)) -> list:
    """CLI から reviewer 群を選ぶファクトリ。観点の異なる 2 体を返す。

    symbol / baseline_params で任意ターゲットに対応（既定は同梱 dedupe）。
    """
    if kind == "mock":
        return [ReviewerMock("safety", symbol, baseline_params),
                ReviewerMock("scope", symbol, baseline_params)]
    if kind == "cli-run":
        return [ReviewerCliRun("safety", symbol), ReviewerCliRun("scope", symbol)]
    raise ValueError(f"unknown reviewers: {kind!r}（mock|cli-run）")
