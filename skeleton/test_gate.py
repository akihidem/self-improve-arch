"""決定的な核の証明: 採否 = 計測値（LLM 非依存）。

builder.MOCK_CANDIDATES の 3 候補を、実 sandbox（隔離適用 + 実テスト + 実ベンチ）
に通し、gate.evaluate_gates の戻り値だけで採否を確認する。

  correct_fast -> adopt=True   （正しい かつ 有意に高速）
  wrong_fast   -> adopt=False  （テスト不合格＝順序が壊れる）
  null         -> adopt=False  （主要KPI 未達＝baseline と同等で改善なし）

ここに LLM/builder の自己申告は一切登場しない。採否は sandbox の実測と
gate の決定的ロジックのみで決まる。
"""
import sandbox
from builder import MOCK_CANDIDATES
from gate import evaluate_gates


def _gate_for(name: str):
    src = MOCK_CANDIDATES[name].source
    sb = sandbox.evaluate_candidate(src)
    gate = evaluate_gates(
        judge_approved=True,                 # ゲート床を単体検証（Reviewer/Judge 層は test_judge.py）
        tests_passed=sb.tests_passed,
        metrics={"latency": sb.latency},
        primary="latency",
    )
    return sb, gate


def test_correct_fast_is_adopted():
    sb, gate = _gate_for("correct_fast")
    assert sb.tests_passed is True, sb.test_output
    assert gate.adopt is True, gate.reasons
    # 採用理由が「実ベンチの有意改善」であることを確認
    d = gate.detail["latency"]
    assert d["improved"] and d["significant"] and d["big_enough"], d


def test_wrong_fast_rejected_on_failing_tests():
    sb, gate = _gate_for("wrong_fast")
    assert sb.tests_passed is False  # list(set(...)) は順序を壊す
    assert gate.adopt is False
    assert any("テスト不合格" in r for r in gate.reasons), gate.reasons


def test_null_change_rejected_on_primary_kpi():
    sb, gate = _gate_for("null")
    assert sb.tests_passed is True   # 無変更なので正しさは保たれる
    assert gate.adopt is False       # しかし主要KPI が改善しない
    assert any("主要KPI" in r for r in gate.reasons), gate.reasons
    # 効果量が採用閾値(min_effect=5%)未満であることを確認（null は baseline と同一実装で rel≈0）。
    # ※ 有意性(p<α)は設計上 5% の偽陽性が出るノイズ依存値なので断定しない（旧アサートのフレーク源）。
    assert gate.detail["latency"]["big_enough"] is False
