"""決定的ゲート（採否判定の核）。

出典: improvement_loop_example.py の以下を自己完結でコピーしたもの
（再発明しない）:
  - Metric        (KPI の baseline vs candidate 要約統計)
  - GateResult    (採否 + 理由 + 詳細)
  - two_sample_z  (大標本正規近似の両側 z 検定)
  - evaluate_gates(採用ゲートの本体)

中核方針（improvement_loop_example.py と同一）:
  採否は LLM の自己申告では決めない。テスト実測・KPI 実測・ガードレールという
  「機械的ゲート」が床。judge_approved は必要条件であって十分条件ではない。

このスケルトンは gate-only（walking skeleton）なので、本体は
  「実テスト結果 + 実ベンチの有意差で採否が決まる」
の一点だけを証明する。Reviewer / Judge / 多様性は次マイルストーン。
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field


# --- 出典: improvement_loop_example.py L104-116 (Metric) ---
@dataclass
class Metric:
    """KPI の baseline vs candidate 計測値（要約統計）。"""

    name: str
    baseline_mean: float
    baseline_std: float
    candidate_mean: float
    candidate_std: float
    n: int
    higher_is_better: bool
    is_guardrail: bool = False


# --- 出典: improvement_loop_example.py L118-122 (GateResult) ---
@dataclass
class GateResult:
    adopt: bool
    reasons: list
    detail: dict = field(default_factory=dict)


# --- 出典: improvement_loop_example.py L195-206 (two_sample_z) ---
def two_sample_z(b_mean, b_std, c_mean, c_std, n):
    """大標本の正規近似による両側 z 検定（stdlib のみ）。

    注意: これは簡略版。本番は逐次検定（mSPRT 等）/ 適切な統計ライブラリ /
    多重比較補正（複数提案を同一 baseline と比較する場合）を用いること。
    """
    se = ((b_std ** 2) / n + (c_std ** 2) / n) ** 0.5
    if se == 0:
        return 0.0, 1.0
    z = (c_mean - b_mean) / se
    p = 2 * (1 - statistics.NormalDist().cdf(abs(z)))
    return z, p


# --- 出典: improvement_loop_example.py L209-257 (evaluate_gates) ---
def evaluate_gates(
    *,
    judge_approved: bool,
    tests_passed: bool,
    metrics: dict,
    primary: str,
    within_budget: bool = True,
    alpha: float = 0.05,
    min_effect: float = 0.05,
    guardrail_tol: float = 0.02,
    n_comparisons: int = 1,
) -> GateResult:
    """採用 = Judge承認 AND テスト合格 AND 予算内 AND 主要KPIが有意改善 AND
    ガードレール非回帰。1 つでも欠ければ不採用とし、理由を残す。

    n_comparisons: 同一 baseline に同時比較している候補数（slate サイズ）。複数提案を
        一度に比較して「最良」を採るのは多重検定で、無補正だと family 単位で偽陽性が
        膨張する。Bonferroni で各候補を alpha/n_comparisons で検定し family-wise
        Type-I を alpha 以下に抑える。既定 1（単一候補・無補正）。
    """
    reasons: list = []
    detail: dict = {}

    if not tests_passed:
        reasons.append("テスト不合格")
    if not judge_approved:
        reasons.append("Judge 未承認")
    if not within_budget:
        reasons.append("予算超過")

    # 主要 KPI: 改善方向 かつ 有意 かつ 効果量が閾値以上。
    # 複数候補を同一 baseline と同時比較する場合は Bonferroni で alpha を割る。
    pm = metrics[primary]
    z, p = two_sample_z(pm.baseline_mean, pm.baseline_std, pm.candidate_mean, pm.candidate_std, pm.n)
    rel = (pm.candidate_mean - pm.baseline_mean) / abs(pm.baseline_mean) if pm.baseline_mean else 0.0
    improved = rel > 0 if pm.higher_is_better else rel < 0
    alpha_corrected = alpha / max(1, n_comparisons)
    significant = p < alpha_corrected
    big_enough = abs(rel) >= min_effect
    detail[primary] = {
        "rel": round(rel, 4), "p": round(p, 6), "z": round(z, 3),
        "improved": improved, "significant": significant, "big_enough": big_enough,
        "alpha_used": round(alpha_corrected, 6), "n_comparisons": n_comparisons,
    }
    if not (improved and significant and big_enough):
        reasons.append(f"主要KPI {primary} が改善基準未達 (rel={rel:.3f}, p={p:.4g})")

    # ガードレール（counter-metric）: 1 つでも許容を超えて悪化したら不採用 → Goodhart 対策
    for name, m in metrics.items():
        if not m.is_guardrail:
            continue
        grel = (m.candidate_mean - m.baseline_mean) / abs(m.baseline_mean) if m.baseline_mean else 0.0
        regressed = (grel < -guardrail_tol) if m.higher_is_better else (grel > guardrail_tol)
        detail[name] = {"rel": round(grel, 4), "regressed": regressed, "guardrail": True}
        if regressed:
            reasons.append(f"ガードレール {name} が回帰 (rel={grel:.3f})")

    adopt = len(reasons) == 0
    return GateResult(adopt=adopt, reasons=(["全ゲート通過"] if adopt else reasons), detail=detail)
