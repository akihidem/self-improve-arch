"""候補生成器（Builder）。

Builder は dedupe.py の「全文（候補実装）」を返すだけ。採否には一切関与しない
（採否は sandbox の実測 + gate.evaluate_gates が決める）。多様性のため 1 サイクルで
**複数候補（slate）**を提案する。slate を同一 baseline と同時比較するので、gate は
Bonferroni（alpha/len(slate)）で多重比較補正し、loop は valid な最良候補を 1 つ選ぶ。

- BuilderMock: cycle index で決定的に多様な slate（各 size 3）を返す mock。
    slate 1 -> [correct_fast, wrong_fast, unsafe_default] : valid は correct_fast のみ → 採用
    slate 2 -> [correct_fast, correct_dict, null]         : valid 2つ → 実測 latency で最良を採用
    slate 3 -> [null, unsafe_default, wrong_fast]          : valid なし → 不採用
    以降は循環。temperature / slate_size は LLM builder 用のノブで mock は無視（決定性のため）。
    候補の性質: correct_fast=seen-set O(n) / correct_dict=dict.fromkeys O(n)（共に正しく速い）/
    wrong_fast=list(set())（順序が壊れテスト不合格）/ null=無変更（KPI 改善せず）/
    unsafe_default=共有 mutable default（tests+latency は通るが非 reentrant → Reviewer が veto）。
- BuilderCliRun: claude-cli-run（対話TUI = サブスク枠）を temperature 付きで slate_size 回
    呼び候補を集める。`claude -p` は絶対に使わない（Agent SDK クレジット枠）。この workflow
    内では実走しない（claude-in-claude 回避）。wiring のみ。
"""
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Candidate:
    """Builder が返す候補。dedupe.py を丸ごと置き換えるソース全文 + メタ。"""

    name: str
    source: str  # dedupe.py の全文（dedupe_preserve_order を定義していること）


# --- 候補ソース（dedupe.py 全文）。3 経路を網羅する ---------------------------

CORRECT_FAST = '''\
"""候補: seen-set による O(n) 実装（正しい・順序保持）。"""
from __future__ import annotations


def dedupe_preserve_order(items):
    seen = set()
    result = []
    for x in items:
        if x not in seen:
            seen.add(x)
            result.append(x)
    return result
'''

WRONG_FAST = '''\
"""候補: list(set(...))。速いが set が順序を壊すので不正。"""
from __future__ import annotations


def dedupe_preserve_order(items):
    return list(set(items))
'''

NULL_CHANGE = '''\
"""候補: 無変更（O(n^2) baseline と同一）。主要KPI は改善しない。"""
from __future__ import annotations


def dedupe_preserve_order(items):
    result = []
    for x in items:
        if x not in result:
            result.append(x)
    return result
'''

UNSAFE_DEFAULT = '''\
"""候補: seen-set だが状態を共有 mutable default 引数 _seen に持つ。

単一スレッドでは正しく速い（_seen を毎回 clear するため全テスト合格・O(n)）。
だが _seen は def 時に一度だけ生成され全呼び出しで共有される＝並行/再入で
互いの状態を破壊する。テストも latency ベンチも単一スレッドなので緑になり
（ゲートには不可視）、欠陥はコード上にだけ見える（レビューの必要条件が捕る）。
"""
from __future__ import annotations


def dedupe_preserve_order(items, _seen=set()):
    _seen.clear()
    result = []
    for x in items:
        if x not in _seen:
            _seen.add(x)
            result.append(x)
    return result
'''

CORRECT_DICT = '''\
"""候補: dict.fromkeys による O(n) 実装（正しい・順序保持・Py3.7+）。"""
from __future__ import annotations


def dedupe_preserve_order(items):
    return list(dict.fromkeys(items))
'''

# 名前付き候補（単体テストが名前で直接引けるよう公開）。
_NAMED = {
    "correct_fast": Candidate("correct_fast", CORRECT_FAST),
    "correct_dict": Candidate("correct_dict", CORRECT_DICT),
    "wrong_fast": Candidate("wrong_fast", WRONG_FAST),
    "null": Candidate("null", NULL_CHANGE),
    "unsafe_default": Candidate("unsafe_default", UNSAFE_DEFAULT),
}
MOCK_CANDIDATES = dict(_NAMED)

# 決定的に多様な slate（複数提案）。cycle で循環。各 size 3。
_MOCK_SLATES = [
    ["correct_fast", "wrong_fast", "unsafe_default"],   # valid は correct_fast のみ
    ["correct_fast", "correct_dict", "null"],           # valid 2つ → 実測で最良を選択
    ["null", "unsafe_default", "wrong_fast"],           # valid なし
]


class BuilderMock:
    """cycle index で決定的に候補 slate（複数提案）を返す mock builder。

    多様性は固定の curated slate（各 size 3）。temperature / slate_size は LLM builder
    用のノブで mock では無視する（決定性を保つため）。
    """

    def slate_for_cycle(self, cycle: int, slate_size: int = 3) -> list:
        # cycle は 1 始まり。循環させる。slate_size は mock では無視（固定 slate）。
        names = _MOCK_SLATES[(cycle - 1) % len(_MOCK_SLATES)]
        return [_NAMED[n] for n in names]


class BuilderCliRun:
    """claude-cli-run 経由の実 Builder（wiring のみ。この workflow では実走しない）。

    I/F は ~/.claude/scripts/claude-cli-run.py を確認済み:
      - usage: claude-cli-run [opts] "PROMPT"  (positional prompt or stdin)
      - --permission-mode plan で読取専用
      - 応答（assistant text）を stdout に返す / 失敗は exit!=0 + stderr
    `claude -p` は使わない（対話TUI=cli枠を使う claude-cli-run を使う）。
    不在/失敗は明示 raise（黙って mock に fallback しない）。
    """

    def __init__(self, script_path: str | None = None, model: str | None = None,
                 timeout: int = 300, temperature: float = 0.7):
        self.script_path = script_path or str(
            Path.home() / ".claude" / "scripts" / "claude-cli-run.py"
        )
        self.model = model
        self.timeout = timeout
        self.temperature = temperature   # slate の多様性ノブ（LLM 経路のみ意味を持つ）

    def slate_for_cycle(self, cycle: int, slate_size: int = 3,
                        temperature: float | None = None) -> list:
        """temperature 付きで slate_size 個の多様な候補を集める（wiring のみ）。"""
        temp = self.temperature if temperature is None else temperature
        return [self._one_candidate(i, slate_size, temp) for i in range(slate_size)]

    def _build_prompt(self, idx: int, slate_size: int, temperature: float) -> str:
        return (
            "あなたは Python の最適化担当です。次の関数 dedupe_preserve_order は "
            "出現順を保って重複除去しますが O(n^2) で遅い実装です。\n"
            "正しさ（順序保持・重複除去・空・全重複・hashable 前提・入力を破壊しない）を "
            "維持したままより速い実装を提案してください。\n"
            f"これは多様な {slate_size} 案中の {idx + 1} 案目（temperature≈{temperature}）。"
            "他案と異なるアプローチを取ること。\n"
            "回答は厳密に次の JSON のみ（コードフェンス無し）:\n"
            '{"name": "<短い識別子>", "source": "<dedupe.py の全文。'
            'dedupe_preserve_order を定義すること>"}'
        )

    def _one_candidate(self, idx: int, slate_size: int, temperature: float) -> Candidate:
        if not os.path.exists(self.script_path):
            raise FileNotFoundError(
                f"claude-cli-run が見つからない: {self.script_path}（mock に fallback しない）"
            )
        cmd = [
            "python3", self.script_path,
            "--permission-mode", "plan",   # 読取専用（候補生成にファイル変更は不要）
            "--no-sentinel",
            self._build_prompt(idx, slate_size, temperature),
        ]
        if self.model:
            cmd[3:3] = ["--model", self.model]
        # NOTE: temperature は claude-cli-run がサンプリング flag を公開していれば cmd に
        #       渡す。未確認のため現状はプロンプトに織り込むのみ（実走しない wiring）。
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=self.timeout)
        if proc.returncode != 0:
            raise RuntimeError(
                f"claude-cli-run 失敗 (exit={proc.returncode}): {proc.stderr.strip()[:300]}"
            )
        return self._parse(proc.stdout)

    @staticmethod
    def _parse(stdout: str) -> Candidate:
        text = stdout.strip()
        # 応答に前後の説明が混じる場合に備え、最初の { から最後の } を取り出す
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise ValueError(f"候補 JSON を抽出できない: {text[:200]!r}")
        d = json.loads(text[start:end + 1])
        if "source" not in d:
            raise ValueError(f"候補 JSON に source が無い: {d!r}")
        return Candidate(name=str(d.get("name", "cli-run")), source=str(d["source"]))


class BuilderDir:
    """ディレクトリ内の候補ファイルを 1 サイクル分の slate として読む実 builder。

    claude-in-claude を避けつつ「operator が用意した候補（手書き / 別途 LLM が出力 / 過去案）を
    full rigor（sandbox + Reviewer/Judge + Bonferroni + confirm + query-budget）で採否する」
    実運用経路。各候補ファイルは <module>.py 全文を差し替えるソースで、ファイル名 stem を候補名に
    する。この builder 自体は LLM を repo 内で起動しない（候補は外部で用意済み）。
    """

    def __init__(self, candidates_dir, glob: str = "*.py") -> None:
        self.dir = Path(candidates_dir)
        self.glob = glob

    def slate_for_cycle(self, cycle: int, slate_size: int | None = None) -> list:
        files = sorted(self.dir.glob(self.glob))
        if not files:
            raise FileNotFoundError(f"候補ファイルが無い: {self.dir}/{self.glob}")
        cands = [Candidate(p.stem, p.read_text(encoding="utf-8")) for p in files]
        return cands if slate_size is None else cands[:slate_size]


def make_builder(kind: str, temperature: float = 0.7):
    """CLI から builder を選ぶファクトリ。temperature は LLM 経路の多様性ノブ。"""
    if kind == "mock":
        return BuilderMock()
    if kind == "cli-run":
        return BuilderCliRun(temperature=temperature)
    raise ValueError(f"unknown builder: {kind!r}（mock|cli-run）")
