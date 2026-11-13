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
