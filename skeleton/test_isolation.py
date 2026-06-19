"""OS 隔離（isolation.py）の回帰テスト.

候補の DoS（memory/cpu 暴走）を rlimit バックエンドが実際に封じ込めることを実証する
（host を巻き込まずに contained＝非ゼロ exit で死ぬ）。docker（真の network 境界）は
利用可能な環境でのみ検査し、不可なら skip。
"""
from __future__ import annotations

import sys

import pytest

from isolation import (
    VALID_BACKENDS,
    Limits,
    _docker_usable,
    detect_backend,
    isolation_note,
    run_isolated,
)


def _py(code: str, **kw):
    return run_isolated([sys.executable, "-c", code], cwd="/tmp", timeout=30, **kw)


def test_detect_backend_is_valid():
    assert detect_backend() in VALID_BACKENDS


def test_note_documents_residual():
    # docker 以外は network 非隔離＝真の untrusted には docker/root が要る、と明記する
    assert "docker" in isolation_note("rlimit")
    assert "network" in isolation_note("systemd")
    assert "OS 境界" in isolation_note("docker")


def test_unknown_backend_raises():
    with pytest.raises(ValueError):
        _py("pass", backend="bogus")


def test_none_passthrough_runs():
    p = _py("print('ok')", backend="none")
    assert p.returncode == 0 and "ok" in p.stdout


def test_rlimit_caps_memory():
    # 4GB 確保を試みる → RLIMIT_AS(256MB) でブロックされ contained に死ぬ（host を OOM させない）
    p = _py("bytearray(4_000_000_000)", backend="rlimit",
            limits=Limits(mem_mb=256, cpu_s=20))
    assert p.returncode != 0


def test_rlimit_caps_cpu():
    # 無限ループ → RLIMIT_CPU(2s) で SIGXCPU。timeout(30s) を待たずに死ぬ＝CPU 暴走を封じる
    p = _py("\nwhile True:\n    pass\n", backend="rlimit",
            limits=Limits(mem_mb=256, cpu_s=2))
    assert p.returncode != 0


def test_rlimit_normal_code_still_runs():
    # 上限内の正常コードは普通に通る（隔離が誤って殺さない）
    p = _py("print(sum(range(1000)))", backend="rlimit",
            limits=Limits(mem_mb=256, cpu_s=20))
    assert p.returncode == 0 and "499500" in p.stdout


def test_docker_denies_network_when_available():
    if not _docker_usable():
        pytest.skip("docker 不可の環境（daemon停止/要sudo）— code path のみ")
    code = "import socket; socket.create_connection(('1.1.1.1', 53), timeout=3)"
    p = _py(code, backend="docker")
    assert p.returncode != 0  # --network none で外部接続は失敗する
