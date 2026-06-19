# 継続的自己改善 AI システム — 設計書

Web サービスを継続的に自己改善する AI システムの設計。先行する「生成 → 検証 → 判断」の
決定的オーケストレータを、本番サービスの継続的改善へ拡張したもの。

実装例・構成ファイルは同梱:
`improvement_loop_example.py`（mock で動作検証済み） / `docker-compose.yml` /
`Dockerfile.controller` / `k8s/*.yaml`（全 YAML 妥当性検証済み）。

> **統合注記（2026-06-18・敵対的検品反映）**: 本設計の15装置は **R1（fast/gross/in-band な失敗）** を縛る。連続運転では、(a) 検出器と脅威の「粒度ズレ」で subtle/overfit を内部検出器が捕り損ね、(b) メタ監視層自身が「適応する最適化器 vs 固定された測定装置」を再演し、(c) **construct validity（optimizer vs reality・5つ目の穴）** がフレーミング外に残る、と判明。＝**内部測定には天井**があり、**R2（slow/subtle/construct-drift）は外部アカウンタビリティでしか床にならない**。詳細は §6.5 と `DESIGN-SUPPLEMENT-v2.md`。

---

## 0. 前提：権限境界（この設計で最も重要な不変条件）

「AI は本番を直接変更しない」を**規約ではなく構造で**強制する。AI の書き込み
権限を 3 リングに分け、Sandbox に閉じ込める。

| リング | 主体 | 可能な操作 | 副作用 |
|---|---|---|---|
| **Ring0** | AI 自律 | 改善案生成、Sandbox 実験、KPI 計測、KB 書込 | 外部副作用なし |
| **Ring1** | ゲート付き自動 | Sandbox baseline の更新（Judge承認 + 全ゲート通過時） | **本番影響なし** |
| **Ring2** | 人間ゲート | 本番への昇格（canary→全面） | 本番影響あり。**人間承認必須** |

- 「Judge が承認 → Sandbox 適用」「KPI 改善時のみ採用」は**すべて Ring1 まで**。
- 本番昇格（Ring2）は AI の責務外。AI は PR/manifest を**準備**できるが
  マージ・デプロイはできない（K8s RBAC と GitHub branch protection で強制。§6）。
- これは図の「人間の承認を代替することはできない」の構造的実装。

---

## 1. システムアーキテクチャ

```
                         ┌─────────────────────────────────────────┐
                         │  Improvement Controller (統合AI)         │
                         │  - 改善目的の設定 / スケジューリング      │
                         │  - エージェント並列ディスパッチ           │
                         │  - 予算・ゲート・kill-switch 管理          │
                         └───────┬───────────────────────┬──────────┘
                                 │ ①目的+過去履歴         │ ⑥記録
                                 ▼                         ▲
   ┌───────────┐   ②案    ┌──────────────┐         ┌──────────────────┐
   │ Builder AI│─────────▶│  Reviewer A   │         │  Knowledge Base   │
   │  (AI1生成) │          │ (観点A:正しさ)│──┐      │  - experiments    │
   └───────────┘          └──────────────┘  │③統合 │  - reviews/judg.  │
        ▲                 ┌──────────────┐  ▼      │  - kpi/decisions  │
        │ RAG(過去参照)    │  Reviewer B   │ ┌──────┐│  - artifacts(addr)│
        └──────────────── │ (観点B:価値)  │▶│Judge ││  Postgres+pgvector│
                          └──────────────┘ │(AI4) ││  + S3/MinIO + git │
                                           └──┬───┘└──────────────────┘
                                  ④承認時のみ  │
                                              ▼
   ┌────────────────────── Sandbox（隔離・本番資格情報なし）─────────────┐
   │  apply diff → CI tests → KPI実験(候補 vs baseline / holdout)         │
   │  → ⑤ゲート評価: テスト合格 ∧ 主要KPI有意改善 ∧ ガードレール非回帰     │
   │  → 採用なら Sandbox baseline 更新（Ring1）                            │
   └──────────────────────────────────┬──────────────────────────────────┘
                                       │ Ring2（人間承認 + GitOps）
                                       ▼
   ┌──────────────────────── Production（AI は RBAC を持たない別系統）─────┐
   │  human approve → canary(5%→50%→100%) → guardrail 監視 → 自動rollback  │
   └──────────────────────────────────────────────────────────────────────┘
```

---

## 2. データフロー

```
[trigger: schedule / KPI悪化アラート / 人手]
   │
   ▼
(1) Controller が KB から関連履歴を取得（ベクトル検索 = 「過去に何を試して何が起きたか」）
   │
   ▼
(2) Builder: 目的 + 履歴(RAG) → 改善案 {diff, 仮説, 期待KPI効果(事前宣言), 影響範囲}
   │
   ▼
(3) Reviewer A ∥ Reviewer B（並列・独立・別観点/別モデル）→ それぞれ verdict+findings
   │
   ▼
(4) Judge: 2レビューを統合 → approve / reject / request-changes
   │        （全員 approve でなければ機械的に即 reject）
   │        ※ skeleton 実装は approve/reject の2値（review.JudgeVerdict.approved: bool）。
   │          request-changes（差し戻し）は本設計の将来要素で skeleton には未配線。
   ├── reject ─────────────────────────────────┐
   ▼ approve                                     │
(5) Sandbox 適用（ephemeral env）                 │
   │                                             │
   ▼                                             │
(6) CI: lint → unit/integration tests（JUnit）   │
   │                                             │
   ▼                                             │
(7) KPI 実験: 候補 vs baseline を holdout トラフィックで計測  │
   │                                             │
   ▼                                             │
(8) ゲート評価（決定的・LLM非依存）:               │
     adopt = tests_pass ∧ judge_ok ∧ budget_ok    │
             ∧ 主要KPI有意改善 ∧ ガードレール非回帰  │
   ├── not adopt ──────────────────────┐         │
   ▼ adopt                             │         │
(9) Sandbox baseline 更新（Ring1）       │         │
   │                                   │         │
   ▼                                   ▼         ▼
(10) 全経路を KB へ記録 ←───────────────┴─────────┘
     {案, レビュー, 判定, テスト, KPI実測, ゲート結果, 採否, 理由, 全成果物のhash}
   │
   ▼
(11) [任意] 採用済みを本番昇格候補として PR 化 → Ring2（人間承認 → canary）
```

要点: **KPI とテストの値は生ログ/JUnit から計算し、AI が書ける値は採否の入力にしない**
（先行ループの「スコアは LLM 自己申告ではない」と同じ原則を本番KPIに拡張）。

---

## 3. AI エージェント構成

| エージェント | 図対応 | 入力 | 出力 | モデル方針 |
|---|---|---|---|---|
| **Builder** | AI1 生成 | 目的 + KB履歴(RAG) | diff + 仮説 + **期待KPI効果(事前宣言)** + 影響範囲 | 強モデル |
| **Reviewer A** | AI2 観点A | diff | 正しさ・回帰・安全の指摘 + verdict + risk | 強モデル |
| **Reviewer B** | AI3 観点B | diff | 価値・設計・複雑性の指摘 + verdict + risk | **別モデル**推奨 |
| **Judge** | AI4 判断 | 2レビュー | approve/reject + 根拠 | 強モデル |
| **Controller** | 統合AI | 目的 | サイクル統括・並列・予算・記録 | — |
| **Meta-improver** | （将来） | 過去サイクル群 | エージェント自身のprompt/workflow改善案 | 強モデル + 凍結評価系 |

### Reviewer 独立性の要件（これを満たさないと2体は無意味）

2 体のレビューが価値を持つのは**故障モードが相関しないとき**だけ。同一モデル・
同一プロンプトなら同じ盲点で同時に見落とし、誤った確信を生む。対策:

1. **別観点**: A=正しさ/回帰/安全、B=価値/設計/複雑性。評価ルーブリックを変える。
2. **別モデル系統**が望ましい（例: A=Claude、B=ローカル Ollama）。安価に多様性を得る。
3. Judge は「両者一致」より「**独立した根拠つきの収束**」を重く扱う。単なる一致は弱い証拠。

→ これは三層ガバナンスの「mechanism-layer monoculture risk」がそのまま効く層。
全エージェントが単一モデルだと、改善が**相関した drift** になる。多様性を設計で担保する。

---

## 4. Knowledge Base 設計

目的: (a) Builder への過去文脈供給、(b) 全判断の監査・**再現性**、(c) メタ改善の素地。

### ストレージ構成（役割分離）

| 層 | 技術 | 役割 |
|---|---|---|
| 構造化 | **Postgres** | 実験・レビュー・判定・KPI・採否の関係グラフ。クエリ（「module X を触り採用された案」「KPI Y の履歴」） |
| 意味検索 | **pgvector / Qdrant** | 過去提案・仮説のベクトル検索 = 「似た案を前に試したか」 |
| 成果物 | **S3/MinIO（content-addressed）** | diff/ログ/モデルI/O を hash で保存 → 不変・再現可能 |
| コード真実 | **git** | 各 baseline = commit/tag。KB は SHA を参照（git 単一管理と整合） |

### 主要スキーマ（Postgres 相当・簡約）

```sql
experiments(id, cycle_id, parent_baseline_id, objective, hypothesis,
            expected_primary_rel,            -- 事前宣言した期待効果（p-hacking 抑止）
            builder_model, builder_prompt_ver, diff_sha, created_at, status)

reviews(id, experiment_id, reviewer,         -- 'A' | 'B'
        model, prompt_ver, verdict, risk, findings_json, raw_sha)

judgments(experiment_id, decision, rationale, reviewer_inputs_ref)

test_runs(experiment_id, suite_ver, passed, failed, total, junit_sha)

kpi_measurements(experiment_id, metric, baseline_mean, candidate_mean,
                 delta_rel, ci_low, ci_high, p_value, n,
                 dataset_id,                 -- どの holdout で測ったか
                 is_guardrail)

decisions(experiment_id, adopted, reason, gate_detail_json,
          promoted_to_prod, promoted_by,     -- 本番昇格は人間の id を必ず残す
          rollback_of)

baselines(id, experiment_id, git_sha, created_at)   -- baseline 系譜は DAG
```

### 原則

- **採否の入力は AI が書けないものに限る**（テスト/KPI は生ログ由来、レビューは別記録）。
- **再現に必要な一切を content-addressed で不変保存**（後から監査・差し戻し可能）。
- 採用済みの不良案が将来の提案を歪める「KB 汚染」に備え、**各 entry にゲート証拠を保持** →
  後から系譜単位で隔離できる。

---

## 5. CI/CD 統合

```
Builder が改善案を PR として起票（diff + 仮説）
   │  ※ AI のアカウントは prod ブランチへ merge 権限なし（branch protection）
   ▼
[GitHub Actions / GitLab CI : sandbox パイプライン]
  stage1  lint / 静的解析
  stage2  unit / integration tests（JUnit 出力）
  stage3  sandbox イメージ build → ephemeral env へ deploy
  stage4  KPI 実験ジョブ（候補 vs baseline / holdout）
  stage5  ゲート評価 → 結果を PR check + KB へ書込
   │
   ├─ 全ゲート green → sandbox `main` へ auto-merge（Ring1。本番ではない）
   └─ いずれか赤   → PR にコメント + KB 記録、サイクル終了
   ▼
[本番昇格パイプライン : 別 workflow / 手動トリガ]
  - workflow_dispatch + environment protection（required reviewers = 人間）
  - GitOps（Argo CD/Flux）で prod 同期。AI は prod GitOps repo の保護ブランチに書けない
  - canary → guardrail 監視 → 段階昇格 or 自動 rollback（§9 Argo Rollouts）
```

- 終了コードでゲート: 0=採用 / 1=未採用 / 2=却下 / 3=エラー。
- **シークレットは AI の文脈に入れない**。鍵はパイプライン/Secret が保持し、AI はコードのみ生成。
- 人間承認チャネルは既存のチャットベース承認ゲートを Ring2 に転用可能。

---

## 6. 安全装置（一覧）

| # | 装置 | 防ぐもの |
|---|---|---|
| 1 | **権限リング 0/1/2**（§0） | AI の本番直接変更 |
| 2 | **K8s RBAC / branch protection で境界を強制** | 規約破り（構造で不能化） |
| 3 | **機械的ゲート（テスト/KPI/ガードレール）が床** | AI の「通った」自己申告 |
| 4 | **ガードレール（counter-metric）非回帰** | **Goodhart / reward hacking**（最重要） |
| 5 | **統計的有意 + 事前宣言 + holdout 検証** | ノイズ採用 / p-hacking / 過学習 |
| 6 | **多重比較補正**（N案を同一baselineと比較時） | 偽陽性の累積 |
| 7 | **計測ハーネスの版固定・不変・テスト** | ハーネス自体の仕様ゲーミング |
| 8 | **予算上限**（per-cycle / global、token・$） | コスト暴走（固定クレジットプール対策） |
| 9 | **反復・並列・再帰の上限** | ループ暴走 |
| 10 | **kill-switch / circuit breaker**（事前定義の停止条件 + 人手停止） | 異常の継続 |
| 11 | **ロールバック**（採用は可逆、baseline は DAG、prod は canary 自動 rollback） | 不良の固定化 |
| 12 | **Sandbox 隔離**（egress 遮断 / prod 資格情報なし / ephemeral / quota） | データ流出 / 横展開 |
| 13 | **監査・再現性**（不変 content-addressed KB） | 説明不能な判断 |
| 14 | **Reviewer 多様性要件**（§3） | 相関した盲点 |
| 15 | **メタ改善の追加ゲート**（§付録） | 自己評価器の腐敗（RSI） |

KPI ゲートの正確な定義（「KPI 改善時のみ採用」の honest 版）:

```
adopt = judge_approved
        ∧ tests_passed
        ∧ within_budget
        ∧ 主要KPI が「改善方向 ∧ 統計的有意 ∧ 効果量 ≥ 閾値」
        ∧ すべてのガードレールが「許容内（非回帰）」
```

### 6.5 内部測定の天井（R1/R2）— 敵対的検品で確定（2026-06-18）

§6 の15装置 + 上の KPIゲートは **R1（fast/gross/in-band/異常）** を縛る。連続運転では別系統 **R2（slow/subtle/out-of-band）** が残り、内部測定では原理的に閉じない:

| R2 の穴 | なぜ内部で閉じないか |
|---|---|
| **粒度ズレ** | subtle/overfit を捕る検出器は脅威の事前列挙を要し、列挙できれば既に subtle でない。null カナリアは overfit を、明白 honeypot は subtle poison を捕れない（**検出器が緑でも脅威は通る**）|
| **メタ監視の後退** | 監視層を凍結→進化する最適化器に追い抜かれる／進化させれば RSI 腐敗（付録）。逃げ場のない二択 |
| **construct validity（5つ目の穴・最外周）** | 最適化器が KPI の代理する**現実そのもの**を有害化（依存・ダークパターン・externality）。測定値は緑、悪いのは測ってる世界。内部測定では不可視 |

→ **R2 の床 = 外部アカウンタビリティのみ**: (a) 別アーキ/別ベンダの**異種モデル**（同族盲点を共有しない）/ (b) **人間の価値判断**（automation-bias 承知で）/ (c) **第三者・規制・long-horizon out-of-band 監査**。

**帰結**: 高リスク（臨床/金融/ユーザー福祉直結）サービスに内部 KPI だけで連続自己改善を回さない。§0 Ring2 人間ゲートと付録 RSI「未解決」を堅持＝**外部を内部化しない**。R2 装置の段階導入と honest scope は `DESIGN-SUPPLEMENT-v2.md`。

---

## 7. Python 実装例

同梱 `improvement_loop_example.py`（mock で 2 サイクル動作検証済み）。新規要素
（KPIゲート / 2レビュー並列 / KB）を最小構成で示す。採否の核は LLM 非依存の
`evaluate_gates()`:

```python
adopt = judge_approved ∧ tests_passed ∧ within_budget
        ∧ (主要KPI: improved ∧ significant ∧ big_enough)
        ∧ (各 guardrail: not regressed)
```

**検証で実証されたこと（重要）**:

- cycle1: latency -15%、ガードレール健全 → **ADOPTED_IN_SANDBOX**
- cycle2: latency **-20%（主要KPIは cycle1 より良い）** だが `error_rate` が 3 倍に回帰
  → **NOT_ADOPTED**（ガードレールが「見出しKPIは良いが品質劣化」を阻止）

これが本システムの肝。**主要KPIだけを見れば採用してしまう改善を、ガードレールが止める。**

実行: `python improvement_loop_example.py`（追加依存なし＝標準ライブラリのみ）。

---

## 8. Docker 構成例

同梱 `docker-compose.yml` / `Dockerfile.controller`（YAML 妥当性検証済み）。隔離の要点:

- `sandbox` ネットワークを `internal: true` で外部 egress 遮断。
- `sandbox-web`（被検証サービス）に**本番資格情報を一切渡さない**（合成/匿名化データのみ）。
- `controller` は `platform`（KB）と `sandbox` に到達可、**prod には不可**。
- シークレットは `.env`/外部管理から注入し、イメージに焼き込まない。controller は非 root。

---

## 9. Kubernetes 運用例

同梱 `k8s/*.yaml`（全件 YAML 妥当性検証済み）。境界を**インフラで強制**する点が核心:

- `00-namespaces`: `platform` / `sandbox`。**prod は別 namespace（理想は別クラスタ）**。
- `10-rbac-controller`: Controller の SA は **`sandbox` にのみ**書込 Role を持つ。
  **prod への Role/Binding を意図的に作らない → AI は prod を変更不能**（§0 の構造的強制）。
- `20-controller-cronjob`: 改善サイクルを定期起動（`concurrencyPolicy: Forbid`）。
- `30-experiment-job`: KPI 実験を ephemeral・並列・`activeDeadlineSeconds` 付きで。
- `40-networkpolicy-sandbox`: sandbox の egress を KB と DNS のみに制限。
- `50-resourcequota-sandbox`: `count/jobs.batch: 10` 等で**並列数を物理的にキャップ**。
- `60-prod-rollout-NOTE`: 本番 canary は **Argo Rollouts + AnalysisTemplate**（ガードレール監視で
  自動 rollback）。AI はこの prod manifest の保護ブランチに書けない＝Ring2。

---

## 付録：自己改善（AI 自身の prompt / workflow 改善）の扱い

「将来的に AI 自身を改善対象とする」は**本システム最大のリスク**。理由は単純で、
**変更対象が評価者そのもの**だから——「より良い Judge prompt」が実は甘いだけ、という
変更を、その新 Judge 自身に評価させたら通ってしまう（自己参照ゲートのゲーミング）。

### 封じ込め設計（MVP では完全自動化しない）

1. **凍結評価系**: メタ変更（Builder/Reviewer/Judge の prompt/workflow）は、**版固定・
   変更不能の別ハーネス**と**held-out ベンチ**で評価する。**変更対象が自分を評価しない。**
2. **正解付きベンチ**: 過去実験のうち**採否の正解が分かっているもの**で回帰テスト —
   「新 Judge は、悪いと分かっている案を今も正しく却下し、良い案を承認するか」。
3. **メタ変更は人間承認必須（Ring2+）**。MVP では prompt/workflow の自動採用はしない（hard line）。
4. **メタ深さ = 1**（メタ案がさらにメタ案を自動生成しない）。
5. **champion-challenger + shadow**: 候補 config を実サイクルで shadow 実行し、決定をログのみ
   （非適用）。champion と比較してから人間が昇格判断。

### 正直な限界

安全な RSI は**未解決の研究課題**。本設計は RSI を**封じ込める**（凍結ハーネス + ベンチ +
人間ゲート）のであって**解決**しない。メタ層を有効化する前に、(1)〜(5) が機能することを
製品KPIループ（非メタ）で十分に実証してからにすべき。「pre-registration 凍結」
「kill 条件」といった発想がそのまま適用できる。

> **§6.5 との接続**: 「内部測定の天井」は、この RSI honesty を**システム全体に一般化**したもの——RSI（自己評価器の腐敗）は天井の特殊例、construct validity（optimizer vs reality）が最外周。どちらも結論は同じ＝**外部に出るしかない**。本体が RSI を「未解決」と認め Ring2 を人間に残したのは正しく、誤りは追補 v1 のように*外部を内部に自動化*しようとすること。

---

## 関連パターンとの対応

| パターン | 本設計での再利用 |
|---|---|
| 先行する生成→検証→判断オーケストレータ（決定的スコア） | Sandbox 内ループの核。本設計は KPIゲート/KB/並列/権限境界を足した上位互換 |
| **チャットベースの人間承認ゲート** | Ring2（本番昇格）・メタ変更の人間承認チャネル |
| **git 単一管理** | KB のコード真実層（baseline = commit、KB は SHA 参照） |
| ローカル **Ollama** | Reviewer 多様性（B を別モデルにして相関盲点を減らす）を安価に実現 |
| 固定クレジットプール課金 | 予算上限（§6-8）は任意ではなく必須。深さ・並列は $ で殴られる変数 |
| 三層ガバナンス（mechanism monoculture） | Reviewer 独立性・メタ改善の多様性要件として直結 |
