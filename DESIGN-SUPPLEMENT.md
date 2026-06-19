# DESIGN 追補 — 連続運転における「測定装置の摩耗」対策

本体 DESIGN.md の 15 装置は **per-experiment**（1サイクル・速い・異常な失敗）を縛る。本追補は、それらを**連続時間で回したとき**に効く別系統の失敗——**適応する最適化器が、固定された測定装置を静かに摩耗・ゲーミングしていく**——を塞ぐ。

中核思想: 本体は「各実験を検証する」。追補は「**測定装置そのものの健全性を能動的に監視する**」レイヤー（Measurement-Integrity Monitor, MIM）を1段足す。MIM 自身は本番に触れない（Ring0/1 の権限のまま、Controller に勧告するだけ）。

---

## A. holdout 枯渇 — 測定予算 + フレッシュ確証 + 摩耗カナリア

**問題**: 連続最適化は同じ holdout を何千 diff に叩く＝adaptive data analysis。holdout の癖に過学習した偽の改善を必ず引く。

1. **holdout クエリ予算**: 各 holdout に「採否判断に使える回数」の上限。KB で消費追跡（`holdouts.queries_spent / query_budget`）。枯渇したら retire。
2. **二段 holdout（search ⊥ confirm）**: 探索中に使う holdout と、採用前の最終確証に使う holdout を分離。**confirm holdout は探索に一度も使われていない fresh スライス**。Ring1 採用は confirm holdout で再有意を要求（out-of-sample of the search process）。
3. **Thresholdout（DP 的再利用）**: holdout の生値を返さず「sandbox 推定と有意に違うか」だけをノイズ付きで返し privacy budget を課金（Dwork et al. reusable holdout）。有効クエリ数を伸ばす。
4. **摩耗カナリア（核）**: 既知の null 変更（効果ゼロと分かっている diff）を定期投入し、ゲートが**誤って adopt しないか**を測る。null が通り始めたら測定系が摩耗 → freeze + holdout 更新。← 「測定装置の摩耗を測る層」の実体。

## B. 静的 guardrail vs 適応最適化器 — 広帯域監視 + guardrail 発見 + 原点比較

**問題**: 最適化器は「guard し忘れた次元」を探索する。固定 guardrail はカバレッジの穴を突かれる。

1. **広帯域監視**: 宣言 guardrail に加え、広いメトリクス群（レイテンシ分位・エラー種別・出力分布・リソース・行動プロキシ）を候補評価時に全部 drift 検知。**未宣言の次元が大きく動いたら**、宣言ゲートを通っても「新 guardrail 候補」レビューを発火。
2. **surprise ゲート**: adopt 条件に「Builder が事前宣言した**影響範囲の外**で大きな変化が無いこと」を追加。予測外の動き＝調査対象。
3. **guardrail-gap 監査（敵対的・定期）**: 専任エージェントが「最近の採用群を見て、**測ってない**どの次元に系がドリフトしてるか」を能動的に探す。見つかった穴が新 guardrail になる。
4. **原点比較（slow-drift catcher）**: champion を直前 baseline だけでなく**凍結した歴史的原点**（N ヶ月前の系）と、広い人間判定 eval で定期比較。step-wise guardrail が見逃す累積ドリフトを掴む。

## C. KB-RAG 複利 — 前例の格付け + 負例提示 + 系譜検疫 + ハニーポット

**問題**: ゲートを通った subtle に悪い案が RAG 前例になり、Builder がその上に積む＝系全体が「ゲート通過」に最適化（ゲートへのメタ Goodhart）。

1. **前例の格付け（adopted ≠ confirmed）**: 採用は「暫定」。sandbox baseline で K サイクル**生き残り** + fresh holdout で再確証して初めて「confirmed precedent」（RAG 正例として有効）。最近の採用はまだ強い前例にしない。
2. **RAG の正負バランス**: Builder の検索は durability で重み付けし、**却下/ロールバックされた負例も明示提示**（何が通らないかを学ばせる）。
3. **系譜検疫（能動・推移的）**: ある変更が後で悪いと判明（rollback / guardrail-gap 発覚）したら、それを RAG 子孫に持つ **KB subtree（baselines DAG + 引用 experiments）を自動隔離**し子孫を再評価フラグ。本体の「後で隔離」を rollback トリガの graph-walk に。
4. **anti-Goodhart ハニーポット**: 宣言 KPI を**明らかに悪い機構**で改善する変更（毒入り honeypot）を定期投入し、パイプライン全体（Builder-RAG→Reviewer→Judge→gate）が却下するか検査。通ったら系がゲート・ゲーミングに drift した証拠 → 警報。

## D. 人間 Ring2 のゴム印化 — 承認の genuine 性を構造で強制

**問題**: 人間が見るのは「全ゲート green・KPI +X%」の末端＝automation bias で承認圧最大。構造上ゲートは在るが心理的に劣化。

1. **強制 articulation**: Ring2 UI は「all green」を見出しにしない。承認前に人間が独自に (a) ユーザー体験が何が変わるか (b) ゲートが**測ってない**何が壊れうるか (c) borderline でも承認する理由、を記述しないと承認できない（→ `decisions.reason`）。「green を確認」から「測れてないものを推論」へ。
2. **承認者ハニーポット（較正）**: 承認者に知らせず**既知の悪い・でもゲート green** な変更を Ring2 キューに紛れ込ませる。承認したら automation-bias 失敗を実測 → 「genuine review 率」を追跡。常習ゴム印なら process 変更（承認者ローテ / 二人承認 / cadence 緩和）。
3. **悪魔の代弁者ブリーフ**: 承認前に**独立エージェント（別モデル・敵対プロンプト）**が「これを出すべきでない理由」を書き、人間はそれを読んで反駁してから承認。green フレーミングへの対抗。
4. **cadence 上限 + 承認率監査**: 1セッションの Ring2 承認数に上限（疲労→ゴム印）。承認率監視——**~100% 承認は health でなくゴム印**（健全なゲートは判断で一部の green を蹴る）。

---

## 新レイヤー：Measurement-Integrity Monitor (MIM)

上記は1コンポーネントに集約できる:

- **入力**: KB 全履歴 + 広帯域メトリクス + holdout 消費ログ
- **機能**: holdout 予算/摩耗カナリア(A)・guardrail-gap 監査/原点比較(B)・系譜検疫/honeypot(C)・承認者較正(D)
- **出力**: 「測定系 freeze」「新 guardrail 提案」「KB subtree 隔離」「承認 process 変更」を **Controller に勧告**（MIM は本番に触れない）
- **MIM 自身も版固定・held-out で評価**（RSI 付録の凍結ハーネス原則を MIM に適用＝メタの腐敗を防ぐ）。**MIM の深さも 1**（MIM が MIM を改善しない）。

## KB スキーマ追加（最小）

```sql
holdouts(id, slice_def, role,            -- 'search' | 'confirm'
         queries_spent, query_budget, created_at, retired_at)

precedent_status(experiment_id, status,  -- 'provisional' | 'confirmed' | 'quarantined'
                 survived_cycles, reconfirmed_on_holdout, quarantine_root)

canaries(id, kind,                        -- 'null_wear' | 'goodhart_honeypot' | 'approver_honeypot'
         injected_at, expected_outcome, actual_outcome, passed)

approver_calibration(human_id, honeypots_seen, honeypots_caught,
                     approval_rate, window)
```

（既存 `kpi_measurements.dataset_id` は `holdouts.id` を参照する FK にする。）

## 採否ゲートの拡張（本体 §6 の honest 版に追記）

```
adopt = judge_approved ∧ tests_passed ∧ within_budget
        ∧ 主要KPI（改善 ∧ 有意 ∧ 効果量≥閾値）
        ∧ すべての宣言 guardrail が非回帰
        ∧ confirm-holdout（fresh）で再有意           -- A2
        ∧ 影響範囲外の未宣言次元に大変化なし          -- B2 surprise gate
        ∧ holdout クエリ予算が残っている              -- A1
confirmed_precedent = adopt ∧ survived K cycles ∧ reconfirmed  -- C1
```

## 正直な限界（本体の流儀に合わせて）

- これらは適応最適化 vs 静的測定を**遅らせる**のであって**解決しない**。十分長い地平では canary/honeypot 自体も学習され回避されうる（メタの軍拡）。
- 人間注意の問題は社会技術的——設計で「genuine 性を測る」ことはできるが「genuine にする」ことは強制しきれない。
- MIM を入れる前に、本体の per-experiment ゲートが**非メタの製品 KPI ループ**で十分実証されていること（付録 RSI と同じ順序）。

## 関連パターンとの対応

| 既存 | MIM での再利用 |
|---|---|
| pre-registration 凍結（定量取引）| holdout 予算・摩耗カナリアの「事前宣言」基盤 |
| despike 監査（vendor data）| 広帯域監視の異常次元検知に直結 |
| kill 条件（あるアプリ）| 摩耗カナリア発火時の freeze トリガ |
| 三層ガバナンス（mechanism monoculture）| honeypot/devil's-advocate を別モデルで（Builder と相関させない）|
| チャット承認ゲート | Ring2 の強制 articulation チャネル |
