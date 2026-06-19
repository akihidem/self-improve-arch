# self-improve-arch — walking skeleton（gate + Reviewer/Judge + 多様性 + confirm + 予算）

> はじめての方は **[../docs/EXPLAINER.md](../docs/EXPLAINER.md)**（解説）/ **[../docs/USAGE.md](../docs/USAGE.md)**（使い方）が分かりやすいです。本 README は実装の詳細寄りです。

最小の動く骨組み。**証明する一点だけ**に絞っている:

> 採否（adopt / reject）の床は LLM の自己申告ではなく **実テスト結果 ＋ 実ベンチの
> 有意差**。Reviewer/Judge はその床に **必要条件を足すだけ**（veto 専用・床は動かさない）。
> 複数提案（slate）は同一 baseline と同時比較するので **多重比較補正**し、選んだ最良は
> **探索に未使用の fresh slice で再確証（search ⊥ confirm）**できたものだけ採用する。
> confirm holdout には **query-budget**を課し、枯渇したら（黙って overfit せず）採用を止める。

dedupe は同梱デモだが、**任意の関数を改善対象にして自前の候補ファイルを採否できる**
（→「実際に使う」節 / `examples/first_unique/`）。LLM は repo 内で起動せず、候補は外部で用意した
ファイルを読む（`BuilderDir`）。

## 何をやっているか

改善対象は toy だが本物の関数 `dedupe_preserve_order(items)`
（出現順を保って重複除去）。baseline は `x not in result` を毎回線形走査する
O(n^2) 実装で、多重複の大リストで遅い。

1 サイクル:

```
builder → slate(K 候補) ─┬─ [search workload] 各候補:
                          │     sandbox(隔離適用+実テスト+実ベンチ)
                          │     → Reviewer 2体 → Judge(決定的集約)
                          │     → gate.evaluate_gates(Bonferroni α/K)
                          ├─ valid な最良候補を選択（argmax 改善）
                          ├─ confirm holdout を query-budget から取得（全枯渇なら採用停止）
                          ├─ [fresh confirm slice・別 seed] winner を単一比較(full α)で再評価
                          └─ confirm で再現したものだけ採用 → KB に全候補記録
```

- **builder** は候補（`dedupe.py` の全文）を返すだけ。採否には一切関与しない。
  `BuilderMock` は cycle で決定的に多様な slate（各 size 3）を出す:
  - slate 1 `[correct_fast, wrong_fast, unsafe_default]` → valid は correct_fast のみ → **採用**
  - slate 2 `[correct_fast, correct_dict, null]` → valid 2つ → 実測 latency で**最良を採用**
  - slate 3 `[null, unsafe_default, wrong_fast]` → valid なし → **不採用**
  - 候補: `correct_fast`=seen-set O(n) / `correct_dict`=`list(dict.fromkeys(items))` O(n)（共に正しく速い）/
    `wrong_fast`=`list(set())`（順序が壊れテスト不合格）/ `null`=無変更（KPI 改善せず）/
    `unsafe_default`=共有 mutable default（tests+latency は通るが非 reentrant → Reviewer が veto）。
- **sandbox** は本物の `target/` を `mkdtemp` の隔離コピーに複製し、候補で
  `dedupe.py` を上書きしてから pytest とベンチを実行する。
  **本物の `target/` は実行で mutate しない。** temp は後始末する。
  ベンチは baseline と candidate を同一プロセスで交互計測し mean/std/n を取る。
  `workload_seed` で workload を差し替えられる（search=1234 / confirm=別 seed）。
- **Reviewer 2体 + Judge**（`review.py`）— 床に必要条件を足す層:
  - `ReviewerMock` は観点別の **決定的・コード可視**の検査。`safety` は引数の
    mutable default（非 reentrant な共有状態）を、`scope` は baseline 契約 `(items)`
    を超える公開シグネチャ拡大を blocking 指摘する。KPI 生値は判定に使わない。
  - `Judge` は **決定的 veto ポリシー**（reviewer の blocking が一つでもあれば不承認）。
    LLM 仲裁ではなくコードの結合規則＝監査可能。返す `judge_approved` は gate への一入力。
- **gate**（`gate.py`）は `improvement_loop_example.py` の決定的ロジックを自己完結
  コピー（出典コメント付き・再発明なし）。採否は `evaluate_gates` の戻り値のみ。
  `judge_approved` は **tests/KPI/guardrail と AND** される必要条件で、`True` でも
  tests が落ちれば不採用・`False` なら強制不採用＝**Reviewer/Judge は床を動かせない**。
  slate を同時比較する場合は `n_comparisons=K` で **Bonferroni（α/K）** 補正する。
- **loop**（`loop.py`）は slate を search slice で評価して valid 最良を選び（`_select_winner`）、
  query-budget から confirm holdout を取り（`budget.ConfirmBudget.spend`）、その winner を
  **fresh confirm slice で再確証**（`confirm_winner`・単一比較 full α）。**1 サイクル最大 1 採用**で、
  confirm を通ったものだけ採用する。全 holdout 枯渇なら confirm 不可＝採用停止。
- **KB**（`kb.py`）は sqlite。slate の全候補と confirm holdout の消費（`holdouts` 表）を
  記録（採用フラグは confirm 込みの最終採否、予算消費は run を越えて永続）。`--kb-path` で差替可。

## 多様性・確証・予算（slate / temperature / 多重比較補正 / fresh confirm / query-budget）

- **複数提案（slate）**: 1 サイクルで K 候補を並べて評価し、valid な最良を採る。
- **temperature**: LLM builder（`BuilderCliRun`）の多様性ノブ。mock は決定的な固定 slate
  なので無視する（`--temperature` は cli-run 経路用）。
- **多重比較補正（Bonferroni）**: K 候補を同じ baseline と比較して「最良」を採るのは
  K 回の同時検定。無補正だと family 単位で偽陽性が膨張する（K=5・α=0.05 で「どれかが
  まぐれで有意」確率 ≈ 23%）。各候補を **α/K** で検定して family-wise Type-I を α 以下に抑える。
- **fresh confirm slice（search ⊥ confirm）**: argmax 選択は winner's curse（選ばれた効果量の
  楽観バイアス）を残す。選んだ winner を **探索に一度も使っていない別 seed の workload**で
  **単一比較（full α）**再評価し、**再現したものだけ採用**する。search ノイズに過適合した
  偽の勝者（confirm で効果が消える）はここで弾かれる。
- **query-budget**: confirm slice を毎回叩き続けると longitudinal に holdout が枯渇する
  （adaptive data analysis）。各 slice に「採否判断に使える回数」の上限を持たせ、消費を **KB に
  永続**（`--confirm-budget`）。slice を使い切ったら次の fresh slice へ rotate、pool 全枯渇で
  **採用を止める**。再実行で予算は戻らない（run を越えて効く）。

## honest scope（DESIGN-SUPPLEMENT-v2 の天井と整合）

- **sandbox の AST vet はセキュリティ境界ではない（2026-06-19 独立検証で実証）**: Python では
  `__globals__`/文字列subscript/`getattr` で reflective に任意モジュールへ到達でき、bare
  Name/Attribute/import しか見ない allowlist は追いつかない（検出器の粒度 ≠ 脅威の粒度）。
  任意実行できる候補は計測（perf_counter・直列化・自己申告・早期 os._exit）を forge し、
  **実改善ゼロの候補を計測捏造だけで ADOPT に到達**させられる。本 repo は計測整合性の保証を
  **撤回**し、in-process では「demonstrated な attribute-rebinding の束縛退避」「bench 出力長の
  照合（N 偽装遮断）」という**防御 in depth（境界ではない）**に留める。**本当の床は OS 分離**
  （k8s networkpolicy/RBAC/seccomp）で skeleton では未行使。`test_security.py` がこの境界を固定。
- **query-budget は床ではなく「枯渇の可視化」**: この装置は情報を増やさない。holdout 枯渇を
  **silent な overfit から loud な停止へ変える**だけ。本当の床は **genuinely fresh な外部
  データ源**で、それ自体が有限・非定常（production では作れない）。次の device の Thresholdout
  （DP 的 holdout 再利用）は実効クエリ数を伸ばすが、それも privacy-budget の天井を持つ。
  連続自己改善の内部測定の天井（R2: slow/subtle/construct-drift）はここでも残る。
- **補正/確証が「効く」のは marginal/非再現 effect**: 実 O(n) の勝者は z が巨大（p≈0）で
  Bonferroni では落ちず fresh slice でも再現する。補正が判定を変えるのは marginal な効果、
  confirm が弾くのは search ノイズで勝った非再現の効果で、それぞれ `test_diversity.py`
  （p≈0.03 が α/5 で不採用）/ `test_confirm.py`（confirm で effect が消えると不採用）/
  `test_budget.py`（rotate→枯渇→停止）の合成・in-memory で決定的に実証する。
- `ReviewerMock` は **構造的・コード可視**の欠陥（mutable default・契約外シグネチャ）
  しか捕れない。**subtle/R2（overfit・subtle poison・未観測次元のドリフト・
  construct drift）は捕れない**し、捕れると主張しない（DESIGN-SUPPLEMENT-v2 §0/§2）。
- **Judge は決定的合議**（LLM 仲裁ではない）。LLM の出力は助言入力に留める。
- 実 LLM レビュー（`ReviewerCliRun`）と実 LLM builder（`BuilderCliRun`）は **wiring のみ**。
  自動実行では選ばない（claude-in-claude 回避。`claude -p` ではなく claude-cli-run を呼ぶ設計）。
- 統計は簡略版（大標本正規近似の z 検定 + Bonferroni + 単発 confirm + 単純 query-budget）。
  本番は逐次検定・Šidák/BH-FDR・Thresholdout・genuinely fresh data の供給が要る。

**次マイルストーン（このスケルトンの責務外）:**
- **OS 分離による sandbox 境界**（seccomp / namespace / k8s）— 計測捏造の本当の床。in-process の
  防御 in depth では塞げない（`test_security.py` 参照）。
- `BuilderCliRun` / `ReviewerCliRun` の実走（現状 wiring のみ）
- Thresholdout（DP 的 holdout 再利用）/ genuinely fresh data の供給
- 本番適用（K8s / MIM / prod）。完全に対象外。

## 使い方

```bash
# 全テスト（ゲート床 + 必要条件 + 多重比較補正/選択 + fresh confirm + query-budget + 正しさ）
python3 -m pytest -q

# 個別の証明
python3 -m pytest test_gate.py -q       # 採否=計測値（ゲート床）
python3 -m pytest test_judge.py -q      # 必要条件の不変条件（床は動かない）
python3 -m pytest test_diversity.py -q  # Bonferroni 補正 / 1 サイクル 1 採用
python3 -m pytest test_confirm.py -q    # fresh confirm slice（winner's curse を弾く）
python3 -m pytest test_budget.py -q     # query-budget（rotate → 枯渇 → 停止・永続）

# 通常運転（mock・slate=3）。search 各候補 → confirm（使用 slice）→ 採用/不採用が出る
python3 run.py --builder mock --reviewers mock --slate-size 3 --cycles 3 --kb-path /tmp/kb.sqlite

# 枯渇デモ: budget を絞ると後半サイクルで「holdout 枯渇 → confirm 不可 → 不採用」になる
python3 run.py --confirm-budget 1 --cycles 5 --kb-path /tmp/kb_exhaust.sqlite
```

`--builder cli-run` / `--reviewers cli-run` / `--temperature` は配線済みだが、自動実行では選ばないこと。

## 実際に使う（自分のターゲット × 自分の候補ファイル）

dedupe は同梱デモ。**任意の関数**を改善対象にできる。規約に沿った target dir と候補ファイル群を
用意して run.py に渡すだけ（LLM は repo 内で起動しない＝候補は外部で用意）。

1. **target dir** に 3 ファイルを置く（`examples/first_unique/` が実例）:
   - `<module>.py` … 改善対象（`<symbol>` を定義。現行 baseline 実装）
   - `test_<module>.py` … pytest 正しさテスト（採否の床①）
   - `bench.py` … `make_workload(seed)` と `measure_interleaved(base_fn, cand_fn, data, reps)`
     を `<symbol>` 前提で提供（採否の床②＝実ベンチ・lower-better サンプル列を返す）
2. **候補ディレクトリ**に `<module>.py` 全文の候補を好きなだけ置く（手書き / 別途 LLM 出力 / 過去案。
   `examples/first_unique_candidates/` が実例）。
3. 実行:

```bash
python3 run.py \
  --target-dir examples/first_unique --module first_unique --symbol first_unique --primary latency \
  --candidates-dir examples/first_unique_candidates --cycles 1 --kb-path /tmp/kb.sqlite
# => correct_fast を ADOPT→confirm 再現→採用 / wrong は REJECT(テスト) / noop は改善なしで不採用
```

各候補が sandbox(実テスト + 実ベンチ) → Reviewer/Judge → Bonferroni ゲート → fresh confirm →
query-budget を通り、**valid な最良 1 つだけ**が証拠付きで採否される。KPI が高いほど良いなら
`--higher-is-better`、baseline 引数名が `items` 以外なら `--baseline-params a,b`。

> **安全境界（再掲・重要）**: 候補は untrusted コードとして実行される。信頼できない候補
> （未検証の LLM 出力など）を流すときは **OS 分離（seccomp/namespace/k8s）の中で実行**すること。
> このローカル実行に OS サンドボックスは無い（`sandbox.py` / `test_security.py` 参照）。

## ファイル

| path | 役割 |
|---|---|
| `gate.py` | 決定的ゲート + Bonferroni 多重比較補正（example から自己完結コピー） |
| `review.py` | **Reviewer 2体 + Judge（必要条件・veto 専用）** |
| `budget.py` | **confirm holdout の query-budget（pool rotation・KB 永続）** |
| `target/dedupe.py` | 改善対象（O(n^2) baseline） |
| `target/test_dedupe.py` | 正しさテスト（pytest） |
| `target/bench.py` | 実ベンチ（latency・`make_workload(seed=...)` で slice 切替） |
| `builder.py` | `BuilderMock` / `BuilderCliRun`(wiring) / **`BuilderDir`（実候補ファイルを読む）** |
| `sandbox.py` | 隔離適用 + 実テスト + 実ベンチ。**`Task` で任意ターゲット**（module/symbol/target_dir） |
| `loop.py` | 1 サイクル（slate→search 評価→最良選択→query-budget→fresh confirm→採用） |
| `kb.py` | sqlite KB（候補記録 + holdout 予算の永続追跡） |
| `run.py` | CLI |
| `test_gate.py` | **採否=計測値**（ゲート床）の証明 |
| `test_judge.py` | **必要条件の不変条件**（床は動かない）の証明 |
| `test_diversity.py` | **多重比較補正 / 1 サイクル 1 採用**の証明 |
| `test_confirm.py` | **fresh confirm slice**（winner's curse を弾く）の証明 |
| `test_budget.py` | **query-budget**（rotate → 枯渇 → 停止・永続）の証明 |
| `test_security.py` | **sandbox 整合性の境界**（AST vet は境界でない・捏造遮断・N 偽装棄却）の固定 |
| `test_usable.py` | **任意ターゲット × 実候補で動く**（汎用 Task + BuilderDir）の証明 |
| `examples/first_unique{,_candidates}/` | dedupe 以外の実例ターゲット（first_unique）と実候補ファイル群 |
