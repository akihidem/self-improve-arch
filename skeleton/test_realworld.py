"""実コード適用向け機能（B）の回帰: baseline_params 自動推論 / import allowlist / モジュール定数許可。

いずれも AST レベルで決定的（numpy 等の実インストール不要）。--python の検証は e2e（実トライアル）側。
"""
from pathlib import Path

import pytest

from sandbox import CandidateRejected, Task, infer_baseline_params, vet_candidate_source

_EX = Path(__file__).resolve().parent / "examples" / "first_unique"


# --- baseline_params 自動推論 -------------------------------------------------

def test_infer_baseline_params_from_signature():
    task = Task(target_dir=_EX, module="first_unique", symbol="first_unique")
    assert infer_baseline_params(task) == ("items",)


def test_infer_baseline_params_multi_arg(tmp_path):
    (tmp_path / "m.py").write_text("def f(a, b, c):\n    return a\n", encoding="utf-8")
    assert infer_baseline_params(Task(target_dir=tmp_path, module="m", symbol="f")) == ("a", "b", "c")


def test_infer_baseline_params_missing_symbol(tmp_path):
    (tmp_path / "m.py").write_text("def other(x):\n    return x\n", encoding="utf-8")
    assert infer_baseline_params(Task(target_dir=tmp_path, module="m", symbol="f")) == ()


# --- import allowlist ---------------------------------------------------------

_NUMPY_CAND = "import numpy as np\n\n\ndef f(x):\n    return np.asarray(x)\n"


def test_import_rejected_by_default():
    with pytest.raises(CandidateRejected):
        vet_candidate_source(_NUMPY_CAND, required_func="f")          # 既定は __future__ のみ


def test_import_allowed_when_whitelisted():
    vet_candidate_source(_NUMPY_CAND, required_func="f", allowed_imports=("__future__", "numpy"))


def test_submodule_import_allowed_by_top_module():
    src = "import scipy.linalg as sla\n\n\ndef f(x):\n    return sla.norm(x)\n"
    vet_candidate_source(src, required_func="f", allowed_imports=("__future__", "scipy"))
    with pytest.raises(CandidateRejected):
        vet_candidate_source(src, required_func="f")                  # scipy 未許可なら弾く


# --- モジュール直下の定数代入 -------------------------------------------------

def test_module_level_constant_allowed():
    src = "_K = 5\n_NAMES = ['a', 'b']\n\n\ndef f(x):\n    return x[:_K]\n"
    vet_candidate_source(src, required_func="f")                      # 定数代入は許可


def test_module_level_call_assign_rejected():
    src = "_x = sorted([3, 1])\n\n\ndef f(x):\n    return x\n"
    with pytest.raises(CandidateRejected):
        vet_candidate_source(src, required_func="f")                  # 呼び出し代入は拒否
