<<<<<<< HEAD
"""harness.py - shared utilities for the Parallel Computing lab (Student ID 230103216).

Contains: BenchStats, FNV-1a 64-bit hash, run_benchmark(), emit_verify_tag(),
hardware topology detection (Windows-first, pure standard library) and
small helpers for results files / Markdown tables.

Pure Python 3, standard library only.
"""
import ctypes
import multiprocessing
import os
import platform
import struct
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

STUDENT_ID = "230103216"
SEED = int(STUDENT_ID)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RESULTS_DIR = os.path.join(BASE_DIR, "results")

# --quick (or LAB_QUICK=1) = reduced debug mode. NEVER use it for the final run.
QUICK = ("--quick" in sys.argv) or os.environ.get("LAB_QUICK") == "1"
MODE = "QUICK" if QUICK else "FULL"

BARRIER_TIMEOUT = 900  # seconds; prevents infinite hangs if a worker dies

# --------------------------------------------------------------------------
# FNV-1a 64-bit
# --------------------------------------------------------------------------
FNV64_OFFSET = 0xCBF29CE484222325
FNV64_PRIME = 0x100000001B3
MASK64 = (1 << 64) - 1


def fnv1a_64(data: bytes, h: int = FNV64_OFFSET) -> int:
    for b in data:
        h ^= b
        h = (h * FNV64_PRIME) & MASK64
    return h


# --------------------------------------------------------------------------
# Statistics
# --------------------------------------------------------------------------
def _percentile(sorted_vals: List[float], q: float) -> float:
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = (len(sorted_vals) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo)


@dataclass
class BenchStats:
    samples_ms: List[float]
    median_ms: float
    q1_ms: float
    q3_ms: float
    iqr_ms: float
    min_ms: float
    max_ms: float
    trials: int

    @staticmethod
    def from_ns(samples_ns: List[int]) -> "BenchStats":
        ms = [s / 1e6 for s in samples_ns]
        srt = sorted(ms)
        q1, med, q3 = _percentile(srt, 0.25), _percentile(srt, 0.5), _percentile(srt, 0.75)
        return BenchStats(ms, med, q1, q3, q3 - q1, srt[0], srt[-1], len(ms))


def run_benchmark(fn: Callable[[], object], warmup: int = 1, trials: int = 5) -> BenchStats:
    """Time fn() with perf_counter_ns(); warm-up runs are discarded."""
    for _ in range(warmup):
        fn()
    samples = []
    for _ in range(trials):
        t0 = time.perf_counter_ns()
        fn()
        t1 = time.perf_counter_ns()
        samples.append(t1 - t0)
    return BenchStats.from_ns(samples)


def emit_verify_tag(phase: int, values: List[float], seed: int = SEED) -> str:
    """FNV-1a over '<seed>|PHASE<n>|v1|v2|...' (values with 3 decimals)."""
    payload = f"{seed}|PHASE{phase}|" + "|".join(f"{v:.3f}" for v in values)
    h = fnv1a_64(payload.encode("ascii"))
    tag = f"VERIFY_HASH[PHASE{phase}]={h:016x}"
    print(tag)
    return tag


# --------------------------------------------------------------------------
# Output helpers
# --------------------------------------------------------------------------
def md_table(headers: List[str], rows: List[List[object]]) -> str:
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    for r in rows:
        out.append("| " + " | ".join(str(c) for c in r) + " |")
    return "\n".join(out)


def fmt_bytes(b: Optional[int]) -> str:
    if b is None:
        return "unavailable"
    if b >= 1 << 20 and b % (1 << 20) == 0:
        return f"{b >> 20} MiB"
    if b >= 1 << 10:
        return f"{b / 1024:g} KiB"
    return f"{b} B"


def save_results(phase: int, body: List[str], tag: str) -> str:
    os.makedirs(RESULTS_DIR, exist_ok=True)
    path = os.path.join(RESULTS_DIR, f"phase{phase}_results.txt")
    head = [
        f"MODE: {MODE}" + ("  (reduced debug run - NOT valid for submission)" if QUICK else ""),
        f"Student ID: {STUDENT_ID}",
        f"Python: {platform.python_version()} | OS: {platform.platform()}",
        f"Timestamp: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        "",
    ]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(head + body + ["", tag, ""]))
    print(f"[saved] {path}")
    return path


def get_ctx():
    """Always use 'spawn' so behaviour is identical on Windows and elsewhere."""
    return multiprocessing.get_context("spawn")


# --------------------------------------------------------------------------
# Hardware topology
# --------------------------------------------------------------------------
def _windows_topology(notes: List[str]) -> Dict:
    import ctypes.wintypes as wt

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    fn = k32.GetLogicalProcessorInformationEx
    fn.argtypes = [wt.DWORD, ctypes.c_void_p, ctypes.POINTER(wt.DWORD)]
    fn.restype = wt.BOOL
    length = wt.DWORD(0)
    fn(0xFFFF, None, ctypes.byref(length))  # RelationAll, query size
    if length.value == 0:
        raise OSError("GetLogicalProcessorInformationEx returned no data")
    buf = ctypes.create_string_buffer(length.value)
    if not fn(0xFFFF, buf, ctypes.byref(length)):
        raise OSError(f"GetLogicalProcessorInformationEx failed: {ctypes.get_last_error()}")
    raw = buf.raw[: length.value]
    off, cores, eff, caches = 0, 0, set(), []
    while off < len(raw):
        rel, size = struct.unpack_from("<II", raw, off)
        if size == 0:
            break
        if rel == 0:  # RelationProcessorCore
            _flags, effclass = struct.unpack_from("<BB", raw, off + 8)
            cores += 1
            eff.add(effclass)
        elif rel == 2:  # RelationCache
            level, _assoc, line, csize, ctype = struct.unpack_from("<BBHII", raw, off + 8)
            caches.append((level, ctype, csize, line))  # type: 0 unified,1 instr,2 data
        off += size
    notes.append("Topology source: Win32 GetLogicalProcessorInformationEx (ctypes).")
    return {"physical": cores or None, "caches": caches, "eff": sorted(eff)}


def _windows_model() -> Optional[str]:
    try:
        import winreg
        k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                           r"HARDWARE\DESCRIPTION\System\CentralProcessor\0")
        return winreg.QueryValueEx(k, "ProcessorNameString")[0].strip()
    except Exception:
        return None


def _powershell(cmd: str) -> Optional[str]:
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", cmd],
                           capture_output=True, text=True, timeout=30)
        return r.stdout.strip() or None
    except Exception:
        return None


def _linux_topology(notes: List[str]) -> Dict:
    """Fallback used only when this code is run on non-Windows (for testing)."""
    pairs, pid, caches = set(), None, []
    try:
        for line in open("/proc/cpuinfo"):
            if line.startswith("physical id"):
                pid = line.split(":")[1].strip()
            elif line.startswith("core id"):
                pairs.add((pid, line.split(":")[1].strip()))
    except OSError:
        pass
    base = "/sys/devices/system/cpu/cpu0/cache"
    try:
        for d in sorted(os.listdir(base)):
            if not d.startswith("index"):
                continue
            rd = lambda n: open(os.path.join(base, d, n)).read().strip()
            sz = rd("size")
            mult = {"K": 1024, "M": 1 << 20}.get(sz[-1], 1)
            size = int(sz.rstrip("KM")) * mult
            ctype = {"Data": 2, "Instruction": 1, "Unified": 0}.get(rd("type"), 0)
            caches.append((int(rd("level")), ctype, size, int(rd("coherency_line_size"))))
    except OSError:
        pass
    notes.append("Topology source: /proc/cpuinfo and /sys (non-Windows fallback).")
    return {"physical": len(pairs) or None, "caches": caches, "eff": []}


def detect_topology() -> Dict:
    notes: List[str] = []
    logical = multiprocessing.cpu_count()
    if os.environ.get("PLOG", "").isdigit() and int(os.environ["PLOG"]) > 0:
        logical = int(os.environ["PLOG"])  # test/debug override only
    model, info = None, {"physical": None, "caches": [], "eff": []}
    try:
        if os.name == "nt":
            info = _windows_topology(notes)
            model = _windows_model()
        else:
            info = _linux_topology(notes)
            try:
                for line in open("/proc/cpuinfo"):
                    if line.startswith("model name"):
                        model = line.split(":", 1)[1].strip()
                        break
            except OSError:
                pass
    except Exception as e:  # noqa
        notes.append(f"Primary topology query failed: {e!r}")

    if os.name == "nt":
        if model is None:
            model = _powershell("(Get-CimInstance Win32_Processor | Select-Object -First 1).Name")
            if model:
                notes.append("CPU model via PowerShell Get-CimInstance.")
        if info["physical"] is None:
            out = _powershell("(Get-CimInstance Win32_Processor | Measure-Object "
                              "-Property NumberOfCores -Sum).Sum")
            if out and out.isdigit():
                info["physical"] = int(out)
                notes.append("Physical cores via PowerShell Get-CimInstance (NumberOfCores).")
        if not info["caches"]:
            l2 = _powershell("(Get-CimInstance Win32_Processor | Select-Object -First 1).L2CacheSize")
            l3 = _powershell("(Get-CimInstance Win32_Processor | Select-Object -First 1).L3CacheSize")
            if l2 and l2.isdigit() and int(l2) > 0:
                info["caches"].append((2, 0, int(l2) * 1024, 0))
            if l3 and l3.isdigit() and int(l3) > 0:
                info["caches"].append((3, 0, int(l3) * 1024, 0))
            if info["caches"]:
                notes.append("L2/L3 via PowerShell Win32_Processor (L1d and line size not exposed there).")

    caches = info["caches"]

    def pick(level, types):
        sizes = [c[2] for c in caches if c[0] == level and c[1] in types and c[2] > 0]
        return max(sizes) if sizes else None

    lines = [c[3] for c in caches if c[1] in (0, 2) and c[3] > 0]
    line_size = max(set(lines), key=lines.count) if lines else None
    detail = {}
    for lvl, t, size, ln in caches:
        detail.setdefault((lvl, {0: "unified", 1: "instr", 2: "data", 3: "trace"}.get(t, "?")), set()).add(size)

    physical = info["physical"]
    pphys_env = os.environ.get("PPHYS")
    assumed = False
    if pphys_env and pphys_env.isdigit() and int(pphys_env) > 0:
        pphys = int(pphys_env)
        notes.append(f"Pphys overridden by environment variable PPHYS={pphys}.")
    elif physical:
        pphys = physical
    else:
        pphys, assumed = logical, True
        notes.append("WARNING: physical core count unavailable; Pphys set equal to logical count. "
                     "Set the PPHYS environment variable to the correct value (see README).")
    eff = info["eff"]
    return {
        "cpu_model": model, "logical": logical, "physical": physical,
        "line_size": line_size,
        "l1d": pick(1, (2, 0)), "l2": pick(2, (0, 2)), "l3": pick(3, (0, 2)),
        "cache_detail": {f"L{k[0]} {k[1]}": sorted(v) for k, v in detail.items()},
        "efficiency_classes": eff,
        "hybrid": (len(eff) > 1) if eff else None,
        "Pphys": pphys, "P_log": logical, "pphys_assumed": assumed, "notes": notes,
    }


def topology_table(t: Dict) -> str:
    u = "unavailable"
    rows = [
        ["CPU Model", t["cpu_model"] or u],
        ["Physical cores (Pphys)", str(t["physical"] if t["physical"] is not None else u)
         + (f"  (using Pphys = {t['Pphys']})" if t["physical"] is None or t["Pphys"] != t["physical"] else "")],
        ["Logical threads (P_log)", t["logical"]],
        ["Cache line size", fmt_bytes(t["line_size"])],
        ["L1d size", fmt_bytes(t["l1d"])],
        ["L2 size", fmt_bytes(t["l2"])],
        ["L3 size", fmt_bytes(t["l3"])],
        ["Hybrid (P/E core) CPU", {True: "yes (multiple efficiency classes)", False: "no (single efficiency class)",
                                   None: "unknown"}[t["hybrid"]]],
        ["Python version", platform.python_version()],
        ["Operating system", platform.platform()],
    ]
    return md_table(["Parameter", "Value"], rows)
=======
"""
harness.py - Zero-dependency benchmark, telemetry and verification framework.

Provides:
  * BenchStats / run_benchmark()      - warm-up + timed runs, min/median/IQR/max (ms)
  * bench_stats_from_samples()        - same statistics from externally measured samples
  * fnv1a_hash() / emit_verify_tag()  - manual 64-bit FNV-1a VERIFY_HASH (no hashlib)
  * JSON helpers                      - atomic save/load under results/
  * managed_shared_memory()           - shared memory that is always closed AND unlinked
  * create_synced_pool()/sync_point() - persistent Pool whose tasks start together on
                                        DISTINCT workers (barrier trick)
  * deterministic_doubles()           - reproducible data initialisation
  * get_mode()/pick()                 - full (official) vs quick (debug) parameter switch
  * common_parser()/init_phase()      - shared CLI + Phase-0 state chaining + resume logic

Self-test:      python harness.py --selftest
Recompute hash: python harness.py --recompute phase0
(No import-time side effects, so it is safe under Windows 'spawn'.)
"""
from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import os
import sys
import time
from array import array
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from multiprocessing import shared_memory
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Tuple

PROJECT_DIR = Path(__file__).resolve().parent
RESULTS_DIR = PROJECT_DIR / "results"
DEFAULT_STUDENT_ID = "230103216"          # override with --student-id / LAB_STUDENT_ID

FNV_OFFSET_BASIS = 14695981039346656037
FNV_PRIME = 1099511628211
MASK64 = 0xFFFFFFFFFFFFFFFF


def log(msg: str = "") -> None:
    """print() with immediate flush (so progress is visible in the PyCharm console)."""
    print(msg, flush=True)


# --------------------------------------------------------------------------
# Benchmark statistics
# --------------------------------------------------------------------------
@dataclass
class BenchStats:
    """Summary of timed runs, all values in milliseconds."""
    min_ms: float
    median_ms: float
    iqr_ms: float
    max_ms: float

    def to_dict(self) -> Dict[str, float]:
        return asdict(self)


def fnv1a_hash(s: str) -> int:
    """64-bit FNV-1a hash of a UTF-8 string (exactly as in the assignment)."""
    h = FNV_OFFSET_BASIS
    for b in s.encode("utf-8"):
        h ^= b
        h = (h * FNV_PRIME) & MASK64
    return h


def bench_stats_from_samples(samples_ms: Sequence[float]) -> BenchStats:
    """min / median / IQR / max with the same indexing as the assignment's harness."""
    if len(samples_ms) == 0:
        raise ValueError("no samples")
    s = sorted(samples_ms)
    n = len(s)
    mid = n // 2
    med = s[mid] if n % 2 == 1 else 0.5 * (s[mid - 1] + s[mid])
    q25 = s[n // 4]
    q75 = s[(3 * n) // 4]
    return BenchStats(s[0], med, q75 - q25, s[-1])


def median(values: Sequence[float]) -> float:
    """Median of a non-empty sequence."""
    return bench_stats_from_samples(values).median_ms


def run_benchmark(fn: Callable[[], None], warmup_runs: int = 3, timed_runs: int = 9) -> BenchStats:
    """Run fn() `warmup_runs` times untimed, then `timed_runs` times timed
    (time.perf_counter_ns). Returns wall-clock statistics in ms."""
    if warmup_runs < 0:
        raise ValueError("warmup_runs must be >= 0")
    if timed_runs < 1:
        raise ValueError("timed_runs must be >= 1")
    for _ in range(warmup_runs):
        fn()
    samples: List[float] = []
    for _ in range(timed_runs):
        t0 = time.perf_counter_ns()
        fn()
        t1 = time.perf_counter_ns()
        samples.append((t1 - t0) / 1e6)
    return bench_stats_from_samples(samples)


# --------------------------------------------------------------------------
# VERIFY_HASH
# --------------------------------------------------------------------------
def make_verify_record(task_id: str, student_seed: int, primary_metric: float,
                       secondary_metric: float, p_log: Optional[int] = None) -> Dict[str, Any]:
    """Full audit record. Payload: task_id|seed|P_log|metric1(.4f)|metric2(.4f)."""
    hw = mp.cpu_count() if p_log is None else p_log
    payload = f"{task_id}|{student_seed}|{hw}|{primary_metric:.4f}|{secondary_metric:.4f}"
    sig = fnv1a_hash(payload)
    return {
        "task_id": task_id, "student_seed": student_seed, "p_log": hw,
        "metric1": round(primary_metric, 4), "metric2": round(secondary_metric, 4),
        "payload": payload, "hash": f"{sig:016X}",
        "tag": f"VERIFY_HASH[{task_id}]: {sig:016X} (P_log={hw})",
    }


def emit_verify_tag(task_id: str, student_seed: int,
                    primary_metric: float, secondary_metric: float) -> str:
    """Console line 'VERIFY_HASH[<id>]: <16 hex> (P_log=<n>)' (assignment signature)."""
    return make_verify_record(task_id, student_seed, primary_metric, secondary_metric)["tag"]


# --------------------------------------------------------------------------
# Mode switch (official vs. reduced)
# --------------------------------------------------------------------------
def get_mode() -> str:
    """'full' (official assignment parameters) unless LAB_MODE=quick."""
    return "quick" if os.environ.get("LAB_MODE", "full").strip().lower() == "quick" else "full"


def pick(full_value: Any, quick_value: Any) -> Any:
    """Select a parameter according to the current mode."""
    return quick_value if get_mode() == "quick" else full_value


# --------------------------------------------------------------------------
# JSON helpers
# --------------------------------------------------------------------------
def save_json(name: str, data: Dict[str, Any]) -> Path:
    """Atomically write results/<name>.json (temp file + replace)."""
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    path = RESULTS_DIR / f"{name}.json"
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)
    os.replace(tmp, path)
    return path


def load_json(name: str) -> Optional[Dict[str, Any]]:
    """Return parsed results/<name>.json or None if it does not exist / is unreadable."""
    path = RESULTS_DIR / f"{name}.json"
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def load_hardware() -> Dict[str, Any]:
    """Phase 0 state chaining: hardware dict + student_seed. Raises if unusable."""
    data = load_json("phase0")
    if data is None:
        raise RuntimeError("results/phase0.json not found - run task0_cal.py first.")
    hw = dict(data["hardware"])
    if not hw.get("physical_cores"):
        raise RuntimeError("Physical core count unknown. Re-run: "
                           "python task0_cal.py --assume-physical N")
    hw["student_seed"] = data["student_seed"]
    hw["phase0_mode"] = data.get("mode", "full")
    return hw


# --------------------------------------------------------------------------
# Shared CLI / phase initialisation / confirmation
# --------------------------------------------------------------------------
def common_parser(description: str) -> argparse.ArgumentParser:
    """Argument parser shared by all phase scripts."""
    p = argparse.ArgumentParser(description=description)
    p.add_argument("--quick", action="store_true",
                   help="reduced debug workload (results are NOT valid for submission)")
    p.add_argument("--yes", "-y", action="store_true", help="do not ask for confirmation")
    p.add_argument("--force", action="store_true",
                   help="re-run even if results/<phase>.json already exists in this mode")
    return p


def init_phase(args: argparse.Namespace, result_name: str) -> Dict[str, Any]:
    """Apply --quick, load Phase 0 hardware, and implement resume-by-skipping.

    Raises SystemExit(0) if results already exist in the current mode (unless --force).
    Raises RuntimeError if Phase 0 is missing / unusable.
    """
    if args.quick:
        os.environ["LAB_MODE"] = "quick"
    mode = get_mode()
    hw = load_hardware()
    if mode == "full" and hw.get("phase0_mode") == "quick":
        raise RuntimeError("results/phase0.json was produced in QUICK mode. "
                           "Re-run `python task0_cal.py` (full mode) first.")
    if not args.force:
        prev = load_json(result_name)
        if prev and prev.get("mode") == mode:
            log(f"[resume] results/{result_name}.json already exists (mode={mode}); "
                f"nothing to do. Use --force to re-run.")
            raise SystemExit(0)
    return hw


def confirm_run(estimate_text: str, args: argparse.Namespace) -> None:
    """Print the runtime/memory estimate and (full mode) ask before a long benchmark."""
    log(estimate_text)
    if get_mode() == "quick" or args.yes:
        return
    try:
        answer = input("Start the benchmark? [y/N] ")
    except EOFError:
        raise SystemExit("No interactive input available; re-run with --yes.")
    if answer.strip().lower() not in ("y", "yes", "д", "да"):
        raise SystemExit("Aborted by user.")


def balanced_ranges(n: int, parts: int) -> List[Tuple[int, int]]:
    """Split range(n) into `parts` contiguous, nearly equal [start, end) ranges."""
    base, rem = divmod(n, parts)
    out, start = [], 0
    for t in range(parts):
        end = start + base + (1 if t < rem else 0)
        out.append((start, end))
        start = end
    return out


# --------------------------------------------------------------------------
# Safe multiprocessing utilities
# --------------------------------------------------------------------------
@contextmanager
def managed_shared_memory(size: int) -> Iterator[shared_memory.SharedMemory]:
    """Create a SharedMemory block and guarantee close()+unlink() on exit.

    Callers must release any ctypes/memoryview exports of shm.buf before leaving
    the with-block, otherwise close() raises BufferError (reported, not fatal).
    """
    if size <= 0:
        raise ValueError("size must be positive")
    shm = shared_memory.SharedMemory(create=True, size=size)
    try:
        yield shm
    finally:
        try:
            shm.close()
        except BufferError:
            print("[warn] shared memory still has exported buffers; close skipped",
                  file=sys.stderr)
        try:
            shm.unlink()
        except FileNotFoundError:
            pass


_SYNC_BARRIER: Any = None


def _sync_pool_init(barrier: Any, user_init: Optional[Callable[..., None]],
                    user_args: Tuple[Any, ...]) -> None:
    """Pool initializer: stash the barrier, then run the caller's own initializer."""
    global _SYNC_BARRIER
    _SYNC_BARRIER = barrier
    if user_init is not None:
        user_init(*user_args)


def sync_point(timeout: float = 300.0) -> None:
    """Call at the start of a pool task. Blocks until all `processes` workers arrived.

    Because a worker blocked here cannot pick up another task, this guarantees that a
    pool.map(..., chunksize=1) over exactly `processes` tasks runs one task per DISTINCT
    worker, all starting together.
    """
    if _SYNC_BARRIER is None:
        raise RuntimeError("sync_point() used outside a pool made by create_synced_pool()")
    _SYNC_BARRIER.wait(timeout)


def create_synced_pool(processes: int, user_init: Optional[Callable[..., None]] = None,
                       user_args: Tuple[Any, ...] = ()) -> Any:
    """Persistent multiprocessing.Pool whose workers share a start barrier."""
    if processes < 1:
        raise ValueError("processes must be >= 1")
    barrier = mp.Barrier(processes)
    return mp.Pool(processes=processes, initializer=_sync_pool_init,
                   initargs=(barrier, user_init, user_args))


def deterministic_doubles(count: int, seed: int = 12345) -> "array[float]":
    """Reproducible pseudo-random doubles in [0,1) via a 64-bit LCG."""
    if count < 0:
        raise ValueError("count must be >= 0")
    state = seed & MASK64
    out = array("d", bytes(8 * count))
    for i in range(count):
        state = (state * 6364136223846793005 + 1442695040888963407) & MASK64
        out[i] = (state >> 11) / float(1 << 53)
    return out


def render_table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    """Plain-text table for console output."""
    cells = [[str(c) for c in r] for r in rows]
    widths = [max([len(str(h))] + [len(r[i]) for r in cells]) for i, h in enumerate(headers)]
    line = "+" + "+".join("-" * (w + 2) for w in widths) + "+"
    fmt = "| " + " | ".join("{:<%d}" % w for w in widths) + " |"
    out = [line, fmt.format(*headers), line]
    out += [fmt.format(*r) for r in cells]
    out.append(line)
    return "\n".join(out)


def mode_banner() -> None:
    """Warn loudly in quick mode."""
    if get_mode() == "quick":
        log("[warn] QUICK MODE: reduced workload, results/hashes NOT valid for submission.")


# --------------------------------------------------------------------------
# Self-test / recompute CLI
# --------------------------------------------------------------------------
def _selftest() -> int:
    vectors = {"": 0xCBF29CE484222325, "a": 0xAF63DC4C8601EC8C, "foobar": 0x85944171F73967E8}
    ok = True
    for text, expected in vectors.items():
        got = fnv1a_hash(text)
        status = "OK" if got == expected else "FAIL"
        ok &= got == expected
        print(f"FNV-1a({text!r}) = {got:016X}  expected {expected:016X}  [{status}]")
    stats = run_benchmark(lambda: sum(range(1000)), warmup_runs=1, timed_runs=5)
    print("run_benchmark smoke test:", stats)
    return 0 if ok else 1


def _recompute(phase: str) -> int:
    data = load_json(phase)
    if not data or "verify" not in data:
        print(f"No verify record in results/{phase}.json")
        return 1
    v = data["verify"]
    again = f"{fnv1a_hash(v['payload']):016X}"
    print(f"stored    : {v['hash']}\nrecomputed: {again}\n"
          f"{'MATCH' if again == v['hash'] else 'MISMATCH'}")
    return 0 if again == v["hash"] else 1


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    if "--recompute" in sys.argv:
        idx = sys.argv.index("--recompute")
        sys.exit(_recompute(sys.argv[idx + 1] if idx + 1 < len(sys.argv) else "phase0"))
    print(__doc__)
>>>>>>> f97e06c228e1d76b428cbcabc9202be9465db2b5
