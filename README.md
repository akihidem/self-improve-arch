# self-improve-arch

先行する「生成 → 検証 → 判断」の決定的オーケストレータを、本番 web サービスの**継続的自己改善**へ拡張する設計と実装例。**敵対的検品で硬化済み**。

## はじめての方へ（まず読む）

- **[docs/EXPLAINER.md](docs/EXPLAINER.md)** — なにこれ / なぜ / しくみ（予備知識ゼロで読める解説）
- **[docs/USAGE.md](docs/USAGE.md)** — 使い方・手順・テンプレ（自分のコードの改善候補を厳密に採否する）
- **[docs/RSI-LIMITS.md](docs/RSI-LIMITS.md)** — 「自律的な再帰的自己改善（RSI）は実現できる？」→ いいえ・その理由（封じ込めであって実現ではない）
- **[skeleton/](skeleton/)** — 実際に動く実装（任意ターゲット × 実候補ファイルを採否できる）

## 中身

- `DESIGN.md` — 統合版設計（権限リング 0/1/2・決定的ゲート・KB・15 安全装置・RSI 封じ込め）＋ §6.5「内部測定の天井」注記
- `DESIGN-SUPPLEMENT.md` — v1（MIM で穴を「塞ぐ」主張・**敵対的検品で不合格**・記録として残置）
- `DESIGN-SUPPLEMENT-v2.md` — v2（honest 境界地図：R1 は内部で縛れる／R2 は外部アカウンタビリティのみ）
- `improvement_loop_example.py` — 最小実装（stdlib のみ・mock 2 サイクル検証済。採否核 `evaluate_gates()` は LLM 非依存）
- `docker-compose.yml` / `Dockerfile.controller` — ローカル隔離構成
- `k8s/` — 境界をインフラで強制（RBAC で sandbox 限定・networkpolicy で egress 遮断・resourcequota で並列キャップ）
- `skeleton/` — **実際に動く walking skeleton**。採否の床（実テスト+実ベンチ）に Reviewer/Judge・多重比較補正・fresh confirm・query-budget・計測整合性防御を重ね、**任意ターゲット × 実候補ファイルを厳密採否**できる（`docs/USAGE.md`）
- `docs/` — 解説マニュアル（EXPLAINER）と使い方マニュアル（USAGE）

## 核心

1. **「AI は本番を直接変更しない」を規約でなく構造**（K8s RBAC + branch protection）で強制。
2. 採否は**決定的ゲート**（テスト/KPI は生ログ由来・AI が書ける値を採否入力にしない）。
3. **内部測定には天井**がある（§6.5）：検出器と脅威の粒度ズレ・メタ監視の適応 vs 静的・construct validity（optimizer vs reality）。**slow/subtle/construct-drift は外部（異種モデル・人間判断・第三者/規制/long-horizon 監査）でしか床にならない**。

## 開発の経緯（穴出し → 塞ぐ → 検品 → 改訂）

設計 → v1 追補 → 敵対的検品（**核装置が主要脅威を捕れない**と判明）→ v2 で「閉じる」を「天井の地図」に訂正 → 本体 §6.5 に統合。fix より **negative result**（どこまでが内部で守れてどこからが外部か）の方が価値が高かった例。

—

設計記録 + 動く実装（`skeleton/`）。本番適用（OS 分離・実 LLM builder の実走）は別レイヤで対象外。
**信頼できない候補を流すときは OS 分離の中で**（`docs/USAGE.md` 安全境界）。
