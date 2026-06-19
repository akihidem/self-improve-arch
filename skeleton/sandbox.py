"""隔離サンドボックスで候補を検証する。

責務:
  0. 候補ソースを AST 検査し、明白に危険な構文（危険名参照・追加 import・関数定義
     以外のモジュール文）を早期棄却する。**これは防御の第一層であって境界ではない**
     （下記「信頼境界」参照・2026-06-19 検証で訂正）。
  1. 本物の target/ を mkdtemp の隔離コピーへコピー（本物は絶対 mutate しない）。
  2. temp 内の dedupe.py を候補ソースで上書き。
  3. temp 内で pytest を実行し pass/fail を取る（= 実テスト結果）。
  4. ベンチを baseline(元の dedupe.py) と candidate を「同一プロセス内で交互
     (interleave) 計測」し、mean/std/n を Metric(latency, lower better) に
     まとめる（= 実ベンチ）。
  5. temp を後始末。

返り値 SandboxResult.tests_passed / .latency(Metric) が、loop/gate の
「採否=計測値」の入力になる。候補の自己申告は一切使わない。

信頼境界（重要・2026-06-19 独立検証で訂正）:
  候補ソースは builder が完全に支配する untrusted input。**AST 検査はセキュリティ
  境界ではない**: Python では (lambda:0).__globals__["__builtins__"]["__import__"] や
  getattr + 文字列で reflective に任意モジュールへ到達でき、bare Name/Attribute/import
  しか見ない allowlist は原理的に追いつかない（検出器の粒度 ≠ 脅威の粒度）。任意実行
  できる候補は同一/子プロセスの計測（perf_counter・直列化・自己申告・早期 os._exit）を
  forge でき、実改善ゼロの候補を計測捏造だけで ADOPT に到達させられる（検証で実証済）。
  よって AST 検査は「明白な危険の早期棄却」に格下げし、**測定整合性の保証は撤回する**。
  下記 (a)(b) は demonstrated な attribute-rebinding（perf_counter / json.dumps 差し替え）
  の**コストを上げる防御 in depth に過ぎず境界ではない**（gc/frame 走査・os._exit で破れる）。
  **本当の床は OS レベル分離**（DESIGN の k8s networkpolicy/RBAC/seccomp）で、この
  ローカル skeleton では行使していない。
    (a) 計測器（bench.py の perf_counter / runner の json.dumps）を候補ロード前に束縛
        参照へ退避し module 属性の差し替えを無効化する。
    (b) ベンチ出力長を reps と照合（len 不一致＝捏造/破損として棄却）し、同一プロセス
        内交互計測で系統差を相殺する。
"""
from __future__ import annotations

import ast
import json
import shutil
import statistics
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from gate import Metric
from isolation import Limits, run_isolated

# 本物の改善対象。読み取り専用でコピー元にするだけ（mutate 禁止）。
TARGET_DIR = Path(__file__).resolve().parent / "target"

# 候補が定義してよい唯一の公開関数。
REQUIRED_FUNC = "dedupe_preserve_order"

# 候補本体から参照を禁止する名前（計測器・プロセス状態・動的実行への入口）。
# 候補は純粋な dedupe 実装であるべきで、これらは正当な用途を持たない。
_FORBIDDEN_NAMES = frozenset({
    "time", "perf_counter", "perf_counter_ns", "monotonic", "process_time",
    "sys", "os", "subprocess", "importlib", "ctypes", "gc", "threading",
    "__import__", "__builtins__", "__builtin__", "builtins", "globals",
    "eval", "exec", "compile", "open", "exit", "quit",
})
# モジュール先頭で許可する import は __future__ のみ（実装に外部依存は不要）。
_ALLOWED_IMPORT_MODULES = frozenset({"__future__"})


@dataclass(frozen=True)
class Task:
    """改善対象の記述（dedupe 以外の任意ターゲットに向けられるよう汎化）。

    規約: target_dir に `<module>.py`（改善対象・<symbol> を定義）/ `test_<module>.py`
    （pytest）/ `bench.py`（make_workload(seed) と measure_interleaved(base_fn, cand_fn,
    data, reps) を <symbol> 前提で提供）を置く。候補は `<module>.py` 全文を差し替える。
    primary/higher_is_better は採否ゲートの KPI 方向（bench は lower-better のサンプル列を返す前提）。
    """

    target_dir: Path
    module: str = "dedupe"
    symbol: str = "dedupe_preserve_order"
    primary: str = "latency"
    higher_is_better: bool = False
    reps: int = 31
    baseline_params: tuple = ("items",)   # baseline 関数の引数名（scope reviewer の契約判定用）
    python_exe: str = ""                   # テスト/ベンチ subprocess の python（空=sys.executable）。venv 指定用
    allowed_imports: tuple = ("__future__",)  # 候補に許可する import の top-module（信頼候補で numpy 等を許可）
    isolation: str = "rlimit"              # 候補実行の OS 隔離 backend（auto|docker|systemd|rlimit|none）
    mem_mb: int = 1024                     # 隔離時のメモリ上限（MB）
    cpu_s: int = 120                       # 隔離時の CPU 秒上限


# 既定タスク = 同梱の dedupe（後方互換: task 省略時はこれ）。
DEDUPE_TASK = Task(target_dir=TARGET_DIR)


class CandidateRejected(Exception):
    """候補が AST 整合性検査に失格した（採否以前に計測対象から除外）。"""


def _is_literal(node) -> bool:
    """モジュール直下の定数代入として許す値か（定数 / 単項符号付き定数 / 定数のみのリテラル集合）。"""
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, ast.UnaryOp) and isinstance(node.operand, ast.Constant):
        return True
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return all(_is_literal(e) for e in node.elts)
    if isinstance(node, ast.Dict):
        return all(k is not None and _is_literal(k) and _is_literal(v)
                   for k, v in zip(node.keys, node.values))
    return False


def vet_candidate_source(source: str, required_func: str = REQUIRED_FUNC,
                         allowed_imports=_ALLOWED_IMPORT_MODULES) -> None:
    """候補ソースを AST 検査し、明白に危険な構文を早期棄却する（境界ではない・docstring 参照）。

    許可: `from <allowed> import ...` / `import <allowed>`（allowed_imports の top-module。
          既定は __future__ のみ。信頼候補には numpy/scipy 等を渡せる）、docstring、
          モジュール直下の**定数代入**（`_K = 5` 等の設定定数）、required_func を定義する関数定義。
    拒否: 上記以外のモジュール文（副作用ある式文・非定数代入）、禁止名（time/os/eval ...）の参照。
    """
    allowed = frozenset(allowed_imports)

    def _import_ok(names):
        return all(n.split(".")[0] in allowed for n in names)

    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        raise CandidateRejected(f"候補がパースできない: {e}") from e

    defined_funcs = set()
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            defined_funcs.add(node.name)
            continue
        if isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] not in allowed:
                raise CandidateRejected(
                    f"モジュール先頭 import 不許可: from {node.module} import ..."
                )
            continue
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
            if not _import_ok(names):
                raise CandidateRejected(f"モジュール先頭 import 不許可: {names}")
            continue
        # module docstring（定数式文）
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            continue
        # モジュール直下の定数代入（設定定数）だけ許す。呼び出し・名前束縛は拒否。
        if isinstance(node, ast.Assign) and \
                all(isinstance(t, ast.Name) for t in node.targets) and _is_literal(node.value):
            continue
        raise CandidateRejected(
            f"関数定義以外のモジュール文を含む候補は不許可: {type(node).__name__}"
        )

    if required_func not in defined_funcs:
        raise CandidateRejected(f"{required_func} が定義されていない")

    # 関数本体を含む全ノードを走査し、禁止名の参照・属性アクセスを拒否。
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in _FORBIDDEN_NAMES:
            raise CandidateRejected(f"禁止名の参照: {node.id}")
        if isinstance(node, ast.Attribute) and node.attr in _FORBIDDEN_NAMES:
            raise CandidateRejected(f"禁止属性のアクセス: .{node.attr}")
        # import は allowed_imports の top-module のみ許可（それ以外は計測器/プロセスへの入口）。
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
                if not _import_ok(names):
                    raise CandidateRejected(f"候補内 import 不許可: {names}")
            elif (node.module or "").split(".")[0] not in allowed:
                raise CandidateRejected(f"候補内 import 不許可: {node.module}")


@dataclass
class SandboxResult:
    tests_passed: bool
    latency: Metric          # baseline vs candidate（lower better）
    test_output: str = ""    # 失敗時の診断用（末尾のみ）
    rejected: bool = False   # AST 整合性検査で除外された候補か


# baseline と candidate を「同一プロセス内で交互(interleave)計測」する。
# プロセスを分けると warmup / スケジューリングの系統差が実差として latency に
# 乗り、無変更(null)候補でも偶発的に有意改善に見える（reject-path の穴）。
# 同一プロセス・同一 workload で 1 rep ごとに baseline→candidate を交互に呼べば
# その系統差が相殺される。bench.py(=信頼コード)の clock を使い、候補モジュールは
# 「呼び出すだけ」で計測ロジックを支配させない（AST 検査済み前提）。
_BENCH_RUNNER = """\
import json
import importlib.util
import bench

# json.dumps を候補ロード前に束縛退避する。候補が後から json.dumps を setattr で
# 差し替えても _dumps（元の関数オブジェクト）には効かない。防御 in depth であって
# 境界ではない（任意実行できる候補は gc/frame 走査・os._exit で破れる・sandbox docstring 参照）。
_dumps = json.dumps

def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return getattr(mod, {symbol!r})

base = _load({base!r}, "_base_impl")
cand = _load({cand!r}, "_cand_impl")
data = bench.make_workload(seed={seed})
bt, ct = bench.measure_interleaved(base, cand, data, reps={reps})
print(_dumps([bt, ct]))
"""


def _run_bench_interleaved(work_dir: Path, base_impl: Path,
                           cand_impl: Path, reps: int,
                           seed: int = 1234,
                           symbol: str = "dedupe_preserve_order",
                           python_exe: str = "", *,
                           isolation: str = "rlimit",
                           limits: Limits = Limits()) -> tuple[list, list]:
    """同一プロセス内で baseline/candidate を交互計測し (base_timings, cand_timings)。

    work_dir は bench.py を import できるディレクトリ（baseline のコピーで可）。
    baseline と candidate の <module>.py はファイルパスで明示ロードし symbol を取り出す。
    seed は workload の乱数 seed（confirm では search と別 seed = fresh slice）。
    """
    code = _BENCH_RUNNER.format(
        base=str(base_impl), cand=str(cand_impl), reps=reps, seed=seed, symbol=symbol
    )
    proc = run_isolated(
        [python_exe or sys.executable, "-c", code],
        cwd=str(work_dir), timeout=300, backend=isolation, limits=limits,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"bench 失敗: {proc.stderr.strip()[:300]}")
    base_t, cand_t = json.loads(proc.stdout.strip().splitlines()[-1])
    return base_t, cand_t


def _run_tests(work_dir: Path, test_file: str = "test_dedupe.py",
               python_exe: str = "", *, isolation: str = "rlimit",
               limits: Limits = Limits()) -> tuple[bool, str]:
    """work_dir 内で pytest を OS 隔離下で実行。(passed, output 末尾)。python_exe 空なら sys.executable。"""
    proc = run_isolated(
        [python_exe or sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", test_file],
        cwd=str(work_dir), timeout=300, backend=isolation, limits=limits,
    )
    out = (proc.stdout + proc.stderr)[-1200:]
    return proc.returncode == 0, out


def _zero_metric() -> Metric:
    """計測に進めない（拒否された）候補用の中立 Metric（改善ゼロ扱い）。"""
    return Metric(
        name="latency", baseline_mean=1.0, baseline_std=0.0,
        candidate_mean=1.0, candidate_std=0.0, n=1, higher_is_better=False,
    )


def infer_baseline_params(task: Task) -> tuple:
    """baseline 実装（<target_dir>/<module>.py）の symbol 関数の引数名を返す（scope reviewer の契約判定用）。

    --baseline-params を毎回手で渡さずに済むよう、baseline のシグネチャから自動推論する。
    見つからなければ () を返す。
    """
    impl = Path(task.target_dir) / f"{task.module}.py"
    try:
        tree = ast.parse(impl.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return ()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == task.symbol:
            a = node.args
            return tuple(x.arg for x in (list(a.posonlyargs) + list(a.args) + list(a.kwonlyargs)))
    return ()


def evaluate_candidate(candidate_source: str, task: Task = DEDUPE_TASK, *,
                       reps: int | None = None,
                       workload_seed: int = 1234) -> SandboxResult:
    """候補ソースを task.target_dir の隔離コピーに適用し、テスト + ベンチを実測。

    task は改善対象（module/symbol/target_dir/primary/higher_is_better/reps）。省略時は
    同梱 dedupe。最初に AST 整合性検査（task.symbol を要求）。計測器/テスト/プロセスを
    patch しうる候補は実測へ進めず tests_passed=False・改善ゼロで返す（早期棄却）。

    workload_seed はベンチ workload の乱数 seed。search（既定 1234）と別 seed を渡すと
    探索に未使用の fresh confirm slice で再評価できる（winner's curse を弾く）。
    """
    reps = task.reps if reps is None else reps
    impl_name = f"{task.module}.py"
    test_name = f"test_{task.module}.py"

    # --- gate-integrity: untrusted な候補ソースをまず AST 検査（境界ではない・docstring 参照）---
    try:
        vet_candidate_source(candidate_source, task.symbol, task.allowed_imports)
    except CandidateRejected as e:
        return SandboxResult(
            tests_passed=False, latency=_zero_metric(),
            test_output=f"候補が整合性検査で拒否: {e}", rejected=True,
        )

    tmp = Path(tempfile.mkdtemp(prefix="siarch-sbx-"))
    try:
        # --- baseline 用と candidate 用に target_dir を 2 つ隔離コピー ---
        base_dir = tmp / "baseline"
        cand_dir = tmp / "candidate"
        shutil.copytree(task.target_dir, base_dir)
        shutil.copytree(task.target_dir, cand_dir)

        # candidate 側だけ <module>.py を候補で上書き（本物 target_dir は不変）
        (cand_dir / impl_name).write_text(candidate_source, encoding="utf-8")

        limits = Limits(mem_mb=task.mem_mb, cpu_s=task.cpu_s)

        # --- 実テスト（候補に対して・OS 隔離下）---
        tests_passed, test_out = _run_tests(cand_dir, test_name, task.python_exe,
                                            isolation=task.isolation, limits=limits)

        # --- 実ベンチ（同一プロセスで baseline/candidate を交互計測・OS 隔離下）---
        base_t, cand_t = _run_bench_interleaved(
            base_dir, base_dir / impl_name, cand_dir / impl_name, reps,
            seed=workload_seed, symbol=task.symbol, python_exe=task.python_exe,
            isolation=task.isolation, limits=limits,
        )

        # bench 出力長を reps と照合（N 偽装の遮断）。len 不一致は捏造/破損として棄却し、
        # 実データ以上の統計的検出力（n=reps）を主張させない。
        # 注意: reps 長に揃えた「値そのものの捏造」はこの検査では塞げない。候補の任意実行を
        # 断つ OS 分離が無い限り計測は forge 可能（モジュール docstring の信頼境界参照）。
        if not (len(base_t) == len(cand_t) == reps):
            return SandboxResult(
                tests_passed=False, latency=_zero_metric(),
                test_output=(f"bench 出力長が不正 (reps={reps} に対し "
                             f"base={len(base_t)} cand={len(cand_t)}) = 計測捏造/破損の疑い"),
                rejected=True,
            )

        latency = Metric(
            name=task.primary,
            baseline_mean=statistics.mean(base_t),
            baseline_std=statistics.stdev(base_t) if len(base_t) > 1 else 0.0,
            candidate_mean=statistics.mean(cand_t),
            candidate_std=statistics.stdev(cand_t) if len(cand_t) > 1 else 0.0,
            n=reps,
            higher_is_better=task.higher_is_better,
        )
        return SandboxResult(tests_passed=tests_passed, latency=latency,
                             test_output=test_out)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
