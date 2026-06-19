"""採用された勝者を baseline へ昇格し、self-improvement ループを閉じる（最後の1マイル）。

これまで skeleton は「候補を厳密に採否する」までだった（loop.py が adopted を決め KB に記録
するが、勝者を baseline に反映しない＝ループが開いている）。本モジュールは ADOPT を実際に
baseline へ昇格し、次サイクルが「改善後の baseline」の上に積めるようにする
＝ recurse の ChampionPromoteGate（champion.json 差し替え）に相当する昇格段。

Ring 規律（DESIGN.md「AI は prod を直接変更しない」/ DESIGN-SUPPLEMENT-v2 の R2 天井）:
  - mode="staging"（既定・Ring-1 安全）: live baseline は触らず `<module>.promoted.py` に
    「提案された新 baseline」を書くだけ。prod への適用は人間/CI の責務（＝外部アカウンタビリティ。
    R2 の天井は内部で閉じない、という設計の正直さをコードでも守る）。
  - mode="baseline"（ローカル/sandbox の連続改善用）: live baseline を上書きするが、直前を
    `<module>.bak.<sha>.py` に退避して可逆にする。信頼できる隔離環境でのみ使う。
  - mode="none": 何もしない（従来の決定のみ挙動）。

全昇格は KB の promotions 表に from_sha→to_sha・rel・confirm_seed・mode つきで監査記録する。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

PROMOTE_MODES = ("none", "staging", "baseline")


def _sha(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


@dataclass
class PromotionRecord:
    """1 回の昇格の結果（表示・テスト・監査用）。"""

    mode: str
    module: str
    from_sha: str          # 昇格前 baseline の sha（不在なら ""）
    to_sha: str            # 昇格後（勝者ソース）の sha
    primary_rel: float
    confirm_seed: int | None
    path: str              # 書き込んだ先（mode=none は ""）
    backup: str            # baseline モードの退避先（無ければ ""）
    applied: bool          # 実際に書き込んだか


def promote_winner(task, winner_name: str, winner_source: str, *,
                   mode: str = "staging", primary_rel: float = 0.0,
                   confirm_seed: int | None = None, kb=None) -> PromotionRecord:
    """採用された勝者ソースを baseline へ昇格する（mode で Ring を切替）。

    winner_source は `<module>.py` 全文の置き換え（候補規約）。baseline は
    `<task.target_dir>/<task.module>.py`。可逆性のため baseline モードは退避を取る。
    """
    if mode not in PROMOTE_MODES:
        raise ValueError(f"unknown promote mode: {mode!r}（{'|'.join(PROMOTE_MODES)}）")

    target_dir = Path(task.target_dir)
    baseline_path = target_dir / f"{task.module}.py"
    from_sha = _sha(baseline_path.read_text(encoding="utf-8")) if baseline_path.exists() else ""
    to_sha = _sha(winner_source)

    path = ""
    backup = ""
    applied = False

    if mode == "staging":
        # live baseline は触らない。提案された新 baseline を別ファイルに書く（Ring-1 安全）。
        p = target_dir / f"{task.module}.promoted.py"
        p.write_text(winner_source, encoding="utf-8")
        path = str(p)
        applied = True
    elif mode == "baseline":
        # live baseline を上書き。直前を退避して可逆にする（信頼できる隔離環境でのみ）。
        if baseline_path.exists():
            bak = target_dir / f"{task.module}.bak.{from_sha or 'orig'}.py"
            if not bak.exists():
                bak.write_text(baseline_path.read_text(encoding="utf-8"), encoding="utf-8")
            backup = str(bak)
        baseline_path.write_text(winner_source, encoding="utf-8")
        path = str(baseline_path)
        applied = True
    # mode == "none": 何も書かない

    rec = PromotionRecord(
        mode=mode, module=task.module, from_sha=from_sha, to_sha=to_sha,
        primary_rel=float(primary_rel), confirm_seed=confirm_seed,
        path=path, backup=backup, applied=applied,
    )
    if kb is not None and mode != "none":
        kb.record_promotion(
            module=task.module, from_sha=from_sha, to_sha=to_sha,
            primary_rel=float(primary_rel), confirm_seed=confirm_seed,
            mode=mode, path=path,
        )
    return rec
