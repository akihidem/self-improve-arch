# self-improve-arch 使い方マニュアル

「自分のコードの改善候補を、厳密に採否してほしい」人向けの手順書です。
このツールが何で・なぜそうなっているかは [EXPLAINER.md](EXPLAINER.md) を先に読むと早いです。

- 必要なもの: Python 3.10+ と `pytest`（`pip install pytest`）。ネット不要。
- 作業ディレクトリは `skeleton/`（以下のコマンドは全て `skeleton/` 内で実行）。

```bash
cd skeleton
```

---

## 1. まず動かす（30 秒）

```bash
# テストが全部通ることを確認（採否ロジックの自己検証）
python3 -m pytest -q

# 同梱デモ（dedupe）を mock 候補で回す。各候補の採否・confirm・採用 winner が出る
python3 run.py --cycles 3 --kb-path /tmp/demo.sqlite
```

`ADOPT / REJECT / NOT_ADOPTED` と、最後に「採用: ◯◯」または「不採用」が出れば成功です。

---

## 2. 自分のコードを採否する（本番の使い方）

やることは 2 つだけ:「**ターゲットを置く**」「**候補を置く**」。LLM は repo 内で起動しません
（候補は自分で／別途用意したファイルを読みます）。

### 2-1. ターゲットを用意する（3 ファイルの規約）

改善したい関数を `<symbol>`、それを定義するモジュールを `<module>` とします。
`<target-dir>/` に**この 3 ファイル**を置きます（実例: `examples/first_unique/`）。

| ファイル | 役割 |
|---|---|
| `<module>.py` | 改善対象。`<symbol>` を定義した**現行 baseline** 実装 |
| `test_<module>.py` | `pytest` 正しさテスト（採否の床①）。`from <module> import <symbol>` |
| `bench.py` | 速度計測（採否の床②）。下のテンプレ通りに 2 関数を定義 |

`bench.py` のテンプレ（`<symbol>` と workload だけ自分用に変える）:

```python
"""<symbol> の実ベンチ（lower-better のサンプル列を返す規約）。"""
from __future__ import annotations
import time

# 計測クロックを束縛退避（候補の差し替えに耐える。境界ではない・sandbox.py 参照）
_perf_counter = time.perf_counter


def make_workload(size=3000, seed=1234):
    """1 回の計測に渡す入力データを決定的に作る（seed で別データに切替＝confirm 用）。"""
    import random
    rng = random.Random(seed)
    # ↓ あなたの関数が受け取る形のデータを返す（例: 大きいリスト）
    return [rng.randrange(200) for _ in range(size)]


def measure_interleaved(base_fn, cand_fn, data, reps=31):
    """baseline / candidate を 1 rep ごとに交互計測（系統差を相殺）。この本体は変更不要。"""
    base_t, cand_t = [], []
    for i in range(reps):
        if i % 2 == 0:
            t0 = _perf_counter(); base_fn(data); base_t.append(_perf_counter() - t0)
            t0 = _perf_counter(); cand_fn(data); cand_t.append(_perf_counter() - t0)
        else:
            t0 = _perf_counter(); cand_fn(data); cand_t.append(_perf_counter() - t0)
            t0 = _perf_counter(); base_fn(data); base_t.append(_perf_counter() - t0)
    return base_t, cand_t
```

ポイント:
- `<symbol>` は**引数 1 個**（`make_workload` が返す `data` を受け取る）にしておくと楽です。
- workload は「baseline と候補の差が出る」大きさ・形にする（小さすぎると有意差が出ません）。
- KPI が「高いほど良い」なら、ベンチの戻り値が大きいほど良い値になるよう作り、実行時に
  `--higher-is-better` を付けます（既定は lower-better）。

### 2-2. 候補を用意する

`<candidates-dir>/` に、`<module>.py` 全文を差し替える候補ソースを**好きなだけ**置きます
（ファイル名が候補名になる）。出どころは手書き・別ツール・別途 LLM 出力・過去案、何でも可。
実例: `examples/first_unique_candidates/`（`correct_fast.py` / `wrong.py` / `noop.py`）。

> 候補が書ける構文には制限があります（未許可 import・副作用文を禁止／関数定義・定数はOK）。
> numpy 等が要るなら §2-4、弾かれたら §5「AST で弾かれた」を参照。

### 2-3. 実行する

```bash
python3 run.py \
  --target-dir examples/first_unique \
  --module first_unique --symbol first_unique --primary latency \
  --candidates-dir examples/first_unique_candidates \
  --cycles 1 --kb-path /tmp/kb.sqlite
```

この実例だと: `correct_fast` を **ADOPT→confirm 再現→採用** / `wrong` は **REJECT（テスト不合格）** /
`noop` は **改善なしで不採用** になります。

> 「候補を一括で 1 回採否したい」なら `--cycles 1`。`--cycles N` は同じ候補群を N 回まわします
> （confirm 用 query-budget を N 回消費）。

### 2-4. 数値コード（numpy / scipy など・venv 依存）の場合

依存が venv にある／候補が numpy・scipy を使う場合は 2 フラグを足すだけ:

```bash
V=path/to/.venv/bin/python          # 依存(numpy/scipy)の入った python
$V run.py --target-dir mytarget --module mymod --symbol myfn \
  --python "$V" \
  --allow-imports numpy,scipy \
  --candidates-dir mycands --cycles 1 --kb-path /tmp/kb.sqlite
```

- `--python`: テスト/ベンチの subprocess をこの python で実行（venv の依存を解決）。
- `--allow-imports`: 候補に許す import の top-module（`numpy.linalg` は `numpy` でOK）。**候補を更に信頼する操作**なので信頼できる候補にのみ使う。
- `--baseline-params` は通常**不要**（baseline のシグネチャから自動推論）。`K = 5` 等のモジュール直下の定数も候補に書ける。

---

## 3. コマンド・オプション一覧

| オプション | 既定 | 意味 |
|---|---|---|
| `--target-dir DIR` | （同梱 dedupe） | 改善対象 dir（`<module>.py`/`test_<module>.py`/`bench.py`） |
| `--module MOD` | `dedupe` | 改善対象モジュール名（ファイルは `MOD.py`） |
| `--symbol FN` | `dedupe_preserve_order` | 改善対象の関数名 |
| `--primary KPI` | `latency` | 主要 KPI の名前（表示・記録用） |
| `--higher-is-better` | （off=lower） | KPI が高いほど良いとき付ける |
| `--baseline-params a,b` | （自動推論） | baseline 関数の引数名。省略で baseline シグネチャから推論 |
| `--python PATH` | （実行中の python） | テスト/ベンチ subprocess の python。venv 指定で依存(numpy等)解決 |
| `--allow-imports a,b` | `__future__` のみ | 候補に許す import の top-module（例 `numpy,scipy`）。**信頼候補のみ** |
| `--reps N` | `31` | 1 候補あたりの計測 rep 数 |
| `--candidates-dir DIR` | （なし） | 実候補ファイル群。指定すると builder より優先（BuilderDir） |
| `--builder mock\|cli-run` | `mock` | デモ用 builder（candidates-dir 未指定時）。cli-run は自動起動しない |
| `--reviewers mock\|cli-run` | `mock` | レビュアー。cli-run は自動起動しない |
| `--slate-size K` | `0`（=全候補） | 1 サイクルで評価する候補数。0 なら dir の全ファイル |
| `--confirm-budget B` | `3` | confirm 用データ 1 枚あたりの使用回数上限（KB 永続） |
| `--cycles N` | `3` | サイクル数（実候補一括採否なら 1 推奨） |
| `--temperature T` | `0.7` | LLM builder 用の多様性ノブ（mock は無視） |
| `--kb-path PATH` | （リポ内） | 記録 sqlite の場所。デモは `/tmp/...` を推奨 |

---

## 4. 出力の読み方

```
[cycle 1] slate=3候補  search alpha: 0.05 -> 0.01667 (Bonferroni /3)
  ★ correct_fast     ADOPT        tests=True rel=-0.997 p=0.0 significant=True
    noop             NOT_ADOPTED  tests=True rel=0.009 p=0.138 significant=False
        理由: 主要KPI latency が改善基準未達 ...
    wrong            REJECT       tests=False ...
        理由: テスト不合格
  confirm[correct_fast] slice seed=99991: rel=-0.9969 ... -> confirmed=True
  => 採用: correct_fast（search 最良 → confirm 再現）
```

- `★` … その slate で選ばれた最良候補（winner）。
- `ADOPT/REJECT/NOT_ADOPTED` … 候補単体のゲート結果（採用 / テスト不合格 / 基準未達）。
- `rel` … KPI の相対変化（latency は負が改善）。`p` … 有意確率。`significant` … α/K で有意か。
- `review[...] BLOCK` … レビュアーの veto 指摘（テスト緑でも止まる）。
- `confirm[...]` … 別データでの再確認。`confirmed=True` のものだけが最終採用。
- 末尾の `=> 採用 / 不採用` が**そのサイクルの結論**。`confirm holdout 予算` で各データの消費が見えます。
- 全候補の採否・理由は `--kb-path` の sqlite に残ります（`KB 最新` にも一部表示）。

---

## 5. よくある詰まり

- **ベンチが遅い / テストに時間がかかる**: `make_workload` の `size` を下げる、`--reps` を下げる。
- **`significant=False` ばかり（差が出ない）**: workload が小さすぎ。baseline と候補で**明確に差が出る**
  大きさ・性質にする。効果量が小さいと `big_enough`（既定 5%）未満で不採用になります。
- **AST で弾かれた（候補が REJECT 前に rejected）**: 候補に書けるのは docstring・**モジュール直下の
  定数代入**・許可された import（既定 `from __future__` のみ。`--allow-imports` で追加）・関数定義。
  禁止: 副作用ある式文・非定数代入・未許可 import・`time`/`os`/`eval` 等の危険名（計測捏造の早期遮断）。
- **「`<symbol>` が見つからない」**: 候補ファイルが `--symbol` の関数を定義していない、または
  `--symbol` 名が違う。
- **候補が全部 veto される（別ターゲット）**: 通常は `--baseline-params` 自動推論で起きないが、
  baseline と候補の引数名が食い違うと起きる。`--baseline-params a,b` で明示できる。
- **候補が numpy/scipy で弾かれる / 依存が無い**: `--allow-imports numpy,scipy` と、依存の入った
  venv を `--python path/to/.venv/bin/python` で指定（§2-4）。

---

## 6. 安全境界（必読）

**候補は untrusted コードとして実際に実行されます。** AST 検査はセキュリティ境界では
**ありません**（reflective access で回避でき、計測捏造も原理的には可能）。

> **信頼できない候補（未検証の LLM 出力など）を流すときは、必ず OS 分離
> （seccomp / namespace / コンテナ / k8s）の中で実行してください。**
> このローカル実行に OS サンドボックスはありません。

詳細と実証は [`skeleton/sandbox.py`](../skeleton/sandbox.py) の「信頼境界」と
[`skeleton/test_security.py`](../skeleton/test_security.py)。

---

## 7. 新ターゲットを作るチェックリスト

1. `mytarget/myfunc.py` … `def myfunc(data): ...`（現行 baseline）
2. `mytarget/test_myfunc.py` … `from myfunc import myfunc` + 正しさテスト数本
3. `mytarget/bench.py` … §2-1 のテンプレを貼り、`make_workload` を自分のデータ形に変える
4. `mycands/*.py` … 候補（`myfunc` を定義する全文）を 1 つ以上
5. 実行:
   ```bash
   python3 run.py --target-dir mytarget --module myfunc --symbol myfunc \
                  --candidates-dir mycands --cycles 1 --kb-path /tmp/kb.sqlite
   ```
6. （信頼できない候補なら）5 を OS 分離の中で実行。
