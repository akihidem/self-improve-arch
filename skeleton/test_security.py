"""sandbox 整合性の回帰テスト（2026-06-19 独立検証で確定した事実を固定）。

検証結論: AST vet はセキュリティ境界ではない（reflective access で回避可能）。本テストは
  (1) AST vet が reflective exploit を弾けない＝境界でないことを明示（誰かが「vet 強化」で
      塞いだ気にならないよう tripwire として固定。本当の修正は OS 分離）、
  (2) demonstrated な attribute-rebinding 捏造（json.dumps 差し替え）が束縛退避で無効化され
      gate に流れないこと、
  (3) bench 出力長の偽装（N 偽装）が棄却されること、
を固定する。**任意実行できる候補は依然 forge 可能**（gc/frame 走査・os._exit）であり、本テストは
in-process の境界を主張しない。床は OS 分離（DESIGN の k8s/seccomp）。
"""
import sandbox
from builder import MOCK_CANDIDATES
from gate import evaluate_gates
from sandbox import CandidateRejected, vet_candidate_source

# 実装は O(n^2)（実改善ゼロ）。bench の json.dumps を差し替えて baseline~1.0 / candidate~0.001 を
# 吐かせようとする 1b の捏造候補。Name/Attribute/import を一切使わず AST vet を通過する。
_FABRICATE_VIA_DUMPS = '''
def dedupe_preserve_order(items):
    g = (lambda: 0).__globals__
    bi = g["__bui" "ltins__"]
    imp = bi["__imp" "ort__"] if isinstance(bi, dict) else getattr(bi, "__imp" "ort__")
    j = imp("js" "on")
    base = [1.0, 1.002, 0.998, 1.001, 0.999]
    cand = [0.001, 0.0011, 0.0009, 0.00105, 0.00095]
    setattr(j, "du" "mps", lambda *a, **k: str([base, cand]))
    result = []
    for x in items:
        if x not in result:
            result.append(x)
    return result
'''


def test_ast_vet_is_not_a_security_boundary():
    """vet は reflective exploit を弾けない（=境界ではない・仕様）。

    ここで REJECT に変えても別 reflective 経路で抜ける（whack-a-mole）。本当の修正は
    OS 分離であって vet の強化ではない。このテストはその誤解への tripwire。
    """
    try:
        vet_candidate_source(_FABRICATE_VIA_DUMPS)   # 例外を投げない = ACCEPTED
    except CandidateRejected as e:
        raise AssertionError(
            "vet が exploit を弾いた。これは whack-a-mole（別経路で抜ける）。"
            "境界は OS 分離。sandbox.py の信頼境界を参照。"
        ) from e


def test_demonstrated_dumps_fabrication_is_blocked():
    """json.dumps 差し替えによる捏造値が束縛退避(_dumps)で gate に流れない。"""
    sb = sandbox.evaluate_candidate(_FABRICATE_VIA_DUMPS)
    assert sb.rejected is False and sb.tests_passed is True, sb.test_output
    # 捏造が効いていれば baseline_mean==1.0 / candidate_mean==0.001。束縛退避で無効化 → 実測。
    assert abs(sb.latency.baseline_mean - 1.0) > 0.1, \
        ("json.dumps 捏造が依然有効（baseline_mean≈1.0）", sb.latency.baseline_mean)
    assert sb.latency.candidate_mean > 0.001, sb.latency.candidate_mean
    # 実装は O(n^2)（実改善ゼロ）なので捏造を断てば ADOPT されない。
    g = evaluate_gates(judge_approved=True, tests_passed=sb.tests_passed,
                       metrics={"latency": sb.latency}, primary="latency")
    assert g.adopt is False, ("実改善ゼロの候補が ADOPT された", g.reasons, g.detail)


def test_bench_length_mismatch_rejected(monkeypatch):
    """bench 出力長が reps と不一致なら整合性違反として棄却（N 偽装の遮断）。"""
    monkeypatch.setattr(sandbox, "_run_bench_interleaved",
                        lambda *a, **k: ([1.0, 1.0, 1.0], [0.1, 0.1, 0.1]))  # len 3 != reps(31)
    sb = sandbox.evaluate_candidate(MOCK_CANDIDATES["correct_fast"].source)
    assert sb.rejected is True
    assert sb.tests_passed is False
    assert "出力長が不正" in sb.test_output, sb.test_output
