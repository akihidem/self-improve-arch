# 本番デプロイ（K8s）— 「AI は本番を直接変更しない」を構造で強制する

self-improve ループを K8s 上で回す本番形。狙いは**規約でなく構造**で次の 2 つを保証すること:

1. **人間ゲート**（採用は staging まで・prod 反映は人間）を RBAC + branch protection で実体化。
2. **OS/network 分離**（untrusted 候補の exfiltration / escalation / DoS を断つ）を Pod の
   securityContext + NetworkPolicy + rlimit で実体化。

これは [`docs/RSI-LIMITS.md`](../docs/RSI-LIMITS.md) の **Ring2 = 人間ゲート**を K8s に落としたもの。

## マニフェスト

| file | 役割 |
|---|---|
| `k8s/rbac.yaml` | ループ用 SA + 最小 Role。**staging の read/write のみ**・prod 書込権なし（ClusterRole も無し） |
| `k8s/networkpolicy.yaml` | ループ Pod を **network 全遮断**（`--isolation docker --network none` 相当の境界） |
| `k8s/job.yaml` | 1 サイクル Job。runAsNonRoot / ro-rootfs / drop ALL caps / seccomp / 資源上限 |

## どう構造で縛るか

```
ConfigMap(候補/target)  ──mount(network不要)──▶  Job: self-improve ループ
                                                   │  ・SA に prod 書込権なし（RBAC）
                                                   │  ・network 全遮断（NetworkPolicy）
                                                   │  ・非root/ro-rootfs/cap drop/seccomp
                                                   │  ・--apply=staging（prod 直書きしない）
                                                   ▼
                                          staging 成果物（emptyDir/PVC/ConfigMap）
                                                   │
                                       ┌───────────┘  ← ここから先は AI の権限外
                                       ▼
                          CI が staging を読み prod ブランチへ **PR**
                                       │
                          branch protection: **人間レビュー必須**でないと merge 不可
                                       ▼
                                   CD が prod へ反映
```

- ループがどれだけ自律的でも、**prod を自分で書き換える権限が無い**（RBAC）。出口は staging だけ。
- prod ブランチは branch protection（required reviews / required status checks）で守り、**merge は人間**。
- 候補コードは network 遮断 + 非root + ro-rootfs の Pod 内でのみ動く＝計測捏造に成功しても
  外に出られない・権限昇格できない。Pod 内の per-候補 DoS は `--isolation rlimit` が押さえる。

## 適用

```bash
kubectl apply -f deploy/k8s/rbac.yaml
kubectl apply -f deploy/k8s/networkpolicy.yaml
# 候補/target を ConfigMap 化（例）
kubectl -n self-improve create configmap self-improve-input \
  --from-file=candidates/ --from-file=target/
kubectl apply -f deploy/k8s/job.yaml
```

**前提**:
- NetworkPolicy を**強制する CNI**（Calico / Cilium 等）。素の kindnet 等では強制されない＝
  network 境界が無くなるので、その環境では docker（`--isolation docker`）か別 CNI を使うこと。
- image（`ghcr.io/akihidem/self-improve-arch`）に python + pytest + **対象の依存**が入っていること。
- prod ブランチに branch protection（人間レビュー必須）が設定済みであること。**これが無いと
  人間ゲートは成立しない**（最重要前提）。

## honest scope（残る天井）

K8s が縛るのは **実行境界（network/権限/資源）と人間ゲート**であって、**内部測定の天井ではない**:

- 計測値の捏造そのもの（候補が偽の timing を返す）、construct drift（backtest≠実損益）、
  confirm holdout / Thresholdout の privacy budget 枯渇（要 fresh data）は、ここでも残る。
- つまり K8s は「AI に prod を触らせない・候補に外で悪さをさせない」までは**構造**で保証するが、
  「採用された改善が本当に正しい」ことは保証しない。後者は外部 accountability（人間の価値判断・
  第三者監査・新規データ供給）でしか床にならない（[`docs/RSI-LIMITS.md`](../docs/RSI-LIMITS.md)）。

> このマニフェストは**レビュー済みの設計成果物**で、本リポの CI ではクラスタに適用していない
> （クラスタ非依存）。適用は運用者が上記前提を満たした環境で行うこと。
