"""候補コード（信頼できない可能性がある）の実行を OS レベルで隔離する.

なぜ必要か: AST vet は**安全境界ではない**（reflective `__globals__` / 文字列 subscript で
バイパス可能と実証済み）。信頼できない候補に対する**本物の床は OS 分離だけ**。本モジュールは
利用可能な最強バックエンドで subprocess を包む。

バックエンド（強い順）:
  docker  : `--network none` + `--read-only` + cgroup(memory/cpus/pids) + 非 root。
            netns でネットワークを断ち exfiltration まで止める**唯一の真の OS/network 境界**。
            要 docker daemon。image には対象の python/pytest/依存が要る（--docker-image）。
  systemd : `systemd-run --user --scope` の cgroup 資源上限（MemoryMax/CPUQuota/TasksMax）。
            非特権で効く。runaway(memory/fork/cpu) は止めるが **network は隔離しない**。
  rlimit  : `preexec_fn` の setrlimit(AS/CPU/NPROC/FSIZE)。どこでも効く最終手段の DoS 床。
            **network は隔離しない**。
  none    : 生 subprocess（隔離なし）。**信頼できる候補のみ**・明示指定時だけ。

`auto` は docker>rlimit の順に利用可能な最強を選ぶ（systemd は明示指定）。各バックエンドが
**達成した床と残る天井**は `isolation_note()` が一言で返す（README の「装置を足す→残る天井を
明記する」方針）。residual: docker 以外は network 非隔離 ＝ 真の untrusted には docker/root が要る。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass

VALID_BACKENDS = ("auto", "docker", "systemd", "rlimit", "none")


@dataclass(frozen=True)
class Limits:
    """資源上限。candidate の DoS（memory/fork/cpu/file 暴走）を止める。"""

    mem_mb: int = 1024
    cpu_s: int = 120
    pids: int = 256
    fsize_mb: int = 64


_DOCKER_OK: bool | None = None
_SYSTEMD_OK: bool | None = None


def _docker_usable() -> bool:
    global _DOCKER_OK
    if _DOCKER_OK is None:
        _DOCKER_OK = False
        if shutil.which("docker"):
            try:
                _DOCKER_OK = subprocess.run(
                    ["docker", "info"], capture_output=True, timeout=10
                ).returncode == 0
            except Exception:
                _DOCKER_OK = False
    return _DOCKER_OK


def _systemd_usable() -> bool:
    global _SYSTEMD_OK
    if _SYSTEMD_OK is None:
        _SYSTEMD_OK = False
        if shutil.which("systemd-run"):
            try:
                _SYSTEMD_OK = subprocess.run(
                    ["systemd-run", "--user", "--scope", "-q", "true"],
                    capture_output=True, timeout=10
                ).returncode == 0
            except Exception:
                _SYSTEMD_OK = False
    return _SYSTEMD_OK


def detect_backend() -> str:
    """この環境で使える最強バックエンド（auto の既定）。"""
    if _docker_usable():
        return "docker"
    return "rlimit"


def isolation_note(backend: str, network: bool = False) -> str:
    """達成した床と残る天井を一言で。"""
    if backend == "docker":
        net = "network 許可中（要注意）" if network else "network 遮断"
        return f"docker: netns({net})+cgroup+readonly+非root ＝ 真の OS 境界"
    if backend == "systemd":
        return ("systemd cgroup: memory/cpu/pids 上限。⚠ network 非隔離"
                "＝exfiltration は止まらない（真の untrusted には docker/root が要る）")
    if backend == "rlimit":
        return ("rlimit: memory/cpu/proc/file 上限（DoS 床）。⚠ network 非隔離"
                "（真の untrusted には docker/root が要る）")
    if backend == "none":
        return "none: 隔離なしの生実行 ⚠ 信頼できる候補にのみ使うこと"
    return f"unknown backend: {backend}"


def _rlimit_preexec(limits: Limits):
    """fork 後 exec 前に子プロセスへ rlimit を課す preexec_fn を返す（POSIX 専用）。"""
    def _set() -> None:
        import resource
        mem = limits.mem_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
        resource.setrlimit(resource.RLIMIT_CPU, (limits.cpu_s, limits.cpu_s))
        fs = limits.fsize_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_FSIZE, (fs, fs))
        try:
            resource.setrlimit(resource.RLIMIT_NPROC, (limits.pids, limits.pids))
        except (ValueError, OSError):
            pass  # 環境により不可。memory/cpu/file 床は維持される
        os.setsid()  # プロセスグループを分離（暴走子を一括 kill 可能に）
    return _set


def run_isolated(
    cmd: list[str],
    *,
    cwd: str,
    timeout: int,
    backend: str = "auto",
    limits: Limits = Limits(),
    network: bool = False,
    docker_image: str = "python:3.12-slim",
    env: dict | None = None,
) -> subprocess.CompletedProcess:
    """cmd を隔離下で実行し CompletedProcess を返す（capture_output, text）。

    network=False（既定）は docker バックエンドでのみ実効（netns 遮断）。他では
    隔離できないので isolation_note() が警告を出す。
    """
    if backend not in VALID_BACKENDS:
        raise ValueError(f"unknown isolation backend: {backend!r}（{VALID_BACKENDS}）")
    if backend == "auto":
        backend = detect_backend()

    run_env = dict(os.environ if env is None else env)
    run_env.setdefault("PYTHONDONTWRITEBYTECODE", "1")

    if backend == "docker":
        # cmd[0] が host python の絶対パスでも container には無いので "python" に正規化。
        inner = list(cmd)
        if inner and (inner[0] == sys.executable or inner[0].endswith("/python")
                      or inner[0].endswith("/python3")):
            inner[0] = "python"
        wrapped = [
            "docker", "run", "--rm", "-i",
            *(["--network", "none"] if not network else []),
            "--memory", f"{limits.mem_mb}m", "--cpus", "1",
            "--pids-limit", str(limits.pids),
            "--read-only", "--tmpfs", "/tmp:size=64m,exec",
            "-u", f"{os.getuid()}:{os.getgid()}",
            "-v", f"{cwd}:{cwd}:ro", "-w", cwd,
            "-e", "PYTHONDONTWRITEBYTECODE=1",
            docker_image, *inner,
        ]
        return subprocess.run(wrapped, capture_output=True, text=True, timeout=timeout)

    if backend == "systemd":
        wrapped = [
            "systemd-run", "--user", "--scope", "-q",
            "--property", f"MemoryMax={limits.mem_mb}M",
            "--property", f"TasksMax={limits.pids}",
            "--property", "CPUQuota=100%",
            *cmd,
        ]
        return subprocess.run(wrapped, cwd=cwd, capture_output=True, text=True,
                              timeout=timeout, env=run_env)

    if backend == "rlimit":
        return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                              timeout=timeout, preexec_fn=_rlimit_preexec(limits), env=run_env)

    # none
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                          timeout=timeout, env=run_env)
