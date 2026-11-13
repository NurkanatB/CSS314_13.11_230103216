"""
task0_cal.py - Phase 0: hardware topology audit and timer calibration.

Usage (PowerShell, inside parallel_lab/):
    python task0_cal.py                         # uses the default student ID in harness.py
    python task0_cal.py --student-id 230103216
    python task0_cal.py --quick                 # debug only
    python task0_cal.py --assume-physical 8     # ONLY if physical cores are undetectable

Output: console baseline table, VERIFY_HASH[PHASE0] line, results/phase0.json.
Estimated runtime: ~5-15 s (PowerShell start-up dominates on Windows). Memory: negligible.

VERIFY_HASH[PHASE0] inputs (the handout does not specify them; state this in the report):
    metric1 = average perf_counter_ns() call latency in ns (raw, over all reads)
    metric2 = physical core count (P_phys)
"""
from __future__ import annotations

import argparse
import ctypes
import glob
import json
import multiprocessing as mp
import os
import platform
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import harness


# ==========================================================================
# Generic helpers
# ==========================================================================
def run_command(args: List[str], timeout: float = 20.0) -> Optional[str]:
    """Run a command, return stripped stdout or None on any failure."""
    try:
        res = subprocess.run(args, capture_output=True, text=True,
                             timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    out = (res.stdout or "").strip()
    return out if res.returncode == 0 and out else None


def read_text(path: str) -> Optional[str]:
    """Read a small text file, None if unavailable."""
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None


def parse_size_kb(text: Optional[str]) -> Optional[float]:
    """Parse '32K', '1280K', '48 KiB', '12 MiB' into KB (float)."""
    if not text:
        return None
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([KMG])(?:i?B)?\s*", text, re.I)
    if not m:
        return None
    mult = {"K": 1, "M": 1024, "G": 1024 * 1024}[m.group(2).upper()]
    return float(m.group(1)) * mult


def new_hw_record() -> Dict[str, Any]:
    """Empty hardware record; None means 'not detected' (never guessed)."""
    return {
        "os": f"{platform.system()} {platform.release()}",
        "python_os": f"Python {platform.python_version()} / {platform.system()} {platform.release()}",
        "cpu_model": None, "physical_cores": None, "logical_threads": None,
        "cache_line_bytes": None, "l1d_kb": None, "l2_kb": None, "l3_mb": None,
        "sources": {}, "notes": [],
    }


def pick_cache(sizes: List[float], label: str, hw: Dict[str, Any]) -> Optional[float]:
    """Collapse a list of per-instance sizes; note hybrid (differing) sizes."""
    uniq = sorted(set(sizes))
    if not uniq:
        return None
    if len(uniq) > 1:
        hw["notes"].append(f"{label}: differing sizes {uniq} KB (hybrid CPU?); largest reported")
    return uniq[-1]


# ==========================================================================
# Windows: PowerShell/WMI (as in the handout) + kernel32 for per-core caches
# ==========================================================================
class _CacheDesc(ctypes.Structure):
    _fields_ = [("Level", ctypes.c_ubyte), ("Associativity", ctypes.c_ubyte),
                ("LineSize", ctypes.c_ushort), ("Size", ctypes.c_uint32),
                ("Type", ctypes.c_uint32)]


class _SlpiUnion(ctypes.Union):
    _fields_ = [("Cache", _CacheDesc), ("Reserved", ctypes.c_ulonglong * 2)]


class _Slpi(ctypes.Structure):
    """SYSTEM_LOGICAL_PROCESSOR_INFORMATION"""
    _fields_ = [("ProcessorMask", ctypes.c_size_t), ("Relationship", ctypes.c_uint32),
                ("u", _SlpiUnion)]


def windows_topology_via_api() -> Optional[Dict[str, Any]]:
    """GetLogicalProcessorInformation: per-instance cache sizes, line size, core count."""
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        length = ctypes.c_ulong(0)
        kernel32.GetLogicalProcessorInformation(None, ctypes.byref(length))
        if length.value == 0:
            return None
        count = length.value // ctypes.sizeof(_Slpi)
        buf = (_Slpi * count)()
        if not kernel32.GetLogicalProcessorInformation(buf, ctypes.byref(length)):
            return None
    except (OSError, AttributeError):
        return None
    cores, logical = 0, 0
    l1d: List[float] = []
    l2: List[float] = []
    l3: List[float] = []
    lines: List[int] = []
    for entry in buf:
        if entry.Relationship == 0:                       # RelationProcessorCore
            cores += 1
            logical += bin(entry.ProcessorMask).count("1")
        elif entry.Relationship == 2:                     # RelationCache
            c = entry.u.Cache
            kb = c.Size / 1024.0
            if c.Level == 1 and c.Type == 2:              # L1 data
                l1d.append(kb)
                lines.append(c.LineSize)
            elif c.Level == 2 and c.Type in (0, 2):       # L2 unified/data
                l2.append(kb)
            elif c.Level == 3 and c.Type in (0, 2):       # L3 unified/data
                l3.append(kb)
    return {"cores": cores, "logical": logical, "l1d": l1d, "l2": l2, "l3": l3, "lines": lines}


def detect_windows(hw: Dict[str, Any]) -> None:
    """Windows detection: PowerShell Win32_Processor + kernel32 + registry fallback."""
    ps = ("Get-CimInstance Win32_Processor | Select-Object Name,NumberOfCores,"
          "NumberOfLogicalProcessors,L2CacheSize,L3CacheSize | ConvertTo-Json -Compress")
    out = run_command(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], timeout=40)
    wmi_l2_total = wmi_l3_total = None
    if out:
        try:
            parsed = json.loads(out)
            procs = parsed if isinstance(parsed, list) else [parsed]
            hw["cpu_model"] = str(procs[0].get("Name", "")).strip() or None
            hw["physical_cores"] = sum(int(p.get("NumberOfCores") or 0) for p in procs) or None
            hw["logical_threads"] = sum(int(p.get("NumberOfLogicalProcessors") or 0) for p in procs) or None
            wmi_l2_total = procs[0].get("L2CacheSize")
            wmi_l3_total = procs[0].get("L3CacheSize")
            hw["sources"].update(cpu_model="PowerShell Win32_Processor",
                                 physical_cores="PowerShell Win32_Processor",
                                 logical_threads="PowerShell Win32_Processor")
        except (ValueError, TypeError, AttributeError):
            hw["notes"].append("PowerShell output could not be parsed")
    else:
        hw["notes"].append("PowerShell/WMI query failed")

    api = windows_topology_via_api()
    if api:
        if hw["physical_cores"] is None and api["cores"]:
            hw["physical_cores"] = api["cores"]
            hw["sources"]["physical_cores"] = "kernel32 GetLogicalProcessorInformation"
        if hw["logical_threads"] is None and api["logical"]:
            hw["logical_threads"] = api["logical"]
            hw["sources"]["logical_threads"] = "kernel32"
        hw["l1d_kb"] = pick_cache(api["l1d"], "L1d", hw)
        hw["l2_kb"] = pick_cache(api["l2"], "L2 (per instance)", hw)
        l3 = pick_cache(api["l3"], "L3", hw)
        hw["l3_mb"] = (l3 / 1024.0) if l3 else None
        if api["lines"]:
            hw["cache_line_bytes"] = max(api["lines"])
        for k in ("l1d_kb", "l2_kb", "l3_mb", "cache_line_bytes"):
            if hw[k] is not None:
                hw["sources"][k] = "kernel32 GetLogicalProcessorInformation"
    if hw["l2_kb"] is None and wmi_l2_total:
        hw["l2_kb"] = float(wmi_l2_total)
        hw["sources"]["l2_kb"] = "WMI (may be PACKAGE TOTAL)"
    if hw["l3_mb"] is None and wmi_l3_total:
        hw["l3_mb"] = float(wmi_l3_total) / 1024.0
        hw["sources"]["l3_mb"] = "WMI"
    if wmi_l2_total:
        hw["notes"].append(f"WMI L2CacheSize (often package total across cores) = {wmi_l2_total} KB")
    if hw["cpu_model"] is None:
        try:
            import winreg  # stdlib, Windows only
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as k:
                hw["cpu_model"] = winreg.QueryValueEx(k, "ProcessorNameString")[0].strip()
                hw["sources"]["cpu_model"] = "registry"
        except Exception:
            pass


# ==========================================================================
# Linux: /proc/cpuinfo, lscpu, sysfs
# ==========================================================================
def detect_linux(hw: Dict[str, Any]) -> None:
    """Linux detection: /proc/cpuinfo, lscpu, sysfs cache directory, getconf."""
    cpuinfo = read_text("/proc/cpuinfo") or ""
    for key in ("model name", "Model", "Hardware", "Processor"):
        m = re.search(rf"^{key}\s*:\s*(.+)$", cpuinfo, re.M)
        if m:
            hw["cpu_model"] = m.group(1).strip()
            hw["sources"]["cpu_model"] = f"/proc/cpuinfo ({key})"
            break
    lscpu_all = run_command(["lscpu"])
    if hw["cpu_model"] is None and lscpu_all:
        m = re.search(r"^Model name:\s*(.+)$", lscpu_all, re.M)
        if m:
            hw["cpu_model"] = m.group(1).strip()
            hw["sources"]["cpu_model"] = "lscpu"

    hw["logical_threads"] = os.cpu_count()
    hw["sources"]["logical_threads"] = "os.cpu_count()"

    # Physical cores: lscpu -p -> /proc/cpuinfo ids -> sysfs sibling lists
    out = run_command(["lscpu", "-p=CORE,SOCKET"])
    if out:
        pairs = {tuple(l.split(",")[:2]) for l in out.splitlines() if l and not l.startswith("#")}
        if pairs:
            hw["physical_cores"] = len(pairs)
            hw["sources"]["physical_cores"] = "lscpu -p=CORE,SOCKET"
    if hw["physical_cores"] is None:
        phys = re.findall(r"^physical id\s*:\s*(\d+)", cpuinfo, re.M)
        core = re.findall(r"^core id\s*:\s*(\d+)", cpuinfo, re.M)
        if phys and len(phys) == len(core):
            hw["physical_cores"] = len(set(zip(phys, core)))
            hw["sources"]["physical_cores"] = "/proc/cpuinfo ids"
    if hw["physical_cores"] is None:
        sib = {read_text(p) for p in glob.glob("/sys/devices/system/cpu/cpu[0-9]*/topology/thread_siblings_list")}
        sib.discard(None)
        if sib:
            hw["physical_cores"] = len(sib)
            hw["sources"]["physical_cores"] = "sysfs thread_siblings_list"

    # Caches from sysfs (cpu0)
    for idx in sorted(glob.glob("/sys/devices/system/cpu/cpu0/cache/index*")):
        level = read_text(f"{idx}/level")
        ctype = read_text(f"{idx}/type")
        size = parse_size_kb(read_text(f"{idx}/size"))
        line = read_text(f"{idx}/coherency_line_size")
        if level == "1" and ctype == "Data":
            hw["l1d_kb"] = size
            hw["sources"]["l1d_kb"] = "sysfs cpu0"
            if line and line.isdigit():
                hw["cache_line_bytes"] = int(line)
                hw["sources"]["cache_line_bytes"] = "sysfs cpu0"
        elif level == "2" and ctype in ("Unified", "Data"):
            hw["l2_kb"] = size
            hw["sources"]["l2_kb"] = "sysfs cpu0"
        elif level == "3" and ctype in ("Unified", "Data") and size:
            hw["l3_mb"] = size / 1024.0
            hw["sources"]["l3_mb"] = "sysfs cpu0"

    # lscpu fallback (reports totals; divide L1d/L2 by instance count)
    if lscpu_all:
        for key, field in (("L1d", "l1d_kb"), ("L2", "l2_kb"), ("L3", "l3_mb")):
            if hw[field] is not None:
                continue
            m = re.search(rf"^{key} cache:\s*([\d.]+\s*[KMG]i?B?)(?:\s*\((\d+) instance)?", lscpu_all, re.M)
            kb = parse_size_kb(m.group(1)) if m else None
            if kb is None:
                continue
            if key != "L3" and m and m.group(2):
                kb /= int(m.group(2))
            hw[field] = kb / 1024.0 if key == "L3" else kb
            hw["sources"][field] = "lscpu"
    if hw["cache_line_bytes"] is None:
        out = run_command(["getconf", "LEVEL1_DCACHE_LINESIZE"])
        if out and out.isdigit() and int(out) > 0:
            hw["cache_line_bytes"] = int(out)
            hw["sources"]["cache_line_bytes"] = "getconf"


# ==========================================================================
# macOS: sysctl
# ==========================================================================
def _sysctl_int(name: str) -> Optional[int]:
    out = run_command(["sysctl", "-n", name])
    try:
        return int(out) if out else None
    except ValueError:
        return None


def detect_macos(hw: Dict[str, Any]) -> None:
    """macOS detection via sysctl."""
    hw["cpu_model"] = run_command(["sysctl", "-n", "machdep.cpu.brand_string"])
    hw["sources"]["cpu_model"] = "sysctl machdep.cpu.brand_string"
    hw["physical_cores"] = _sysctl_int("hw.physicalcpu")
    hw["logical_threads"] = _sysctl_int("hw.logicalcpu")
    hw["cache_line_bytes"] = _sysctl_int("hw.cachelinesize")
    l1 = _sysctl_int("hw.l1dcachesize") or _sysctl_int("hw.perflevel0.l1dcachesize")
    l2 = _sysctl_int("hw.l2cachesize") or _sysctl_int("hw.perflevel0.l2cachesize")
    l3 = _sysctl_int("hw.l3cachesize")
    hw["l1d_kb"] = l1 / 1024.0 if l1 else None
    hw["l2_kb"] = l2 / 1024.0 if l2 else None
    hw["l3_mb"] = l3 / (1024.0 * 1024.0) if l3 else None
    for k in ("physical_cores", "logical_threads", "cache_line_bytes", "l1d_kb", "l2_kb", "l3_mb"):
        if hw[k] is not None:
            hw["sources"][k] = "sysctl hw.*"
    if hw["cache_line_bytes"] == 128 or (hw["cpu_model"] or "").startswith("Apple"):
        hw["notes"].append("Apple Silicon: hybrid P/E cores; cache values may describe one cluster type")


def detect_hardware() -> Dict[str, Any]:
    """Dispatch to the OS-specific detector; never raises."""
    hw = new_hw_record()
    system = platform.system()
    try:
        if system == "Windows":
            detect_windows(hw)
        elif system == "Linux":
            detect_linux(hw)
        elif system == "Darwin":
            detect_macos(hw)
        else:
            hw["notes"].append(f"Unsupported OS for detailed audit: {system}")
    except Exception as exc:  # defensive: partial results are still useful
        hw["notes"].append(f"Detection error: {exc!r}")
    if hw["cpu_model"] is None:
        hw["cpu_model"] = platform.processor() or None
        if hw["cpu_model"]:
            hw["sources"]["cpu_model"] = "platform.processor()"
    if hw["logical_threads"] is None:
        hw["logical_threads"] = os.cpu_count()
        hw["sources"]["logical_threads"] = "os.cpu_count()"
    return hw


# ==========================================================================
# Timer calibration
# ==========================================================================
def _clock_read_loop(n: int) -> None:
    clock = time.perf_counter_ns
    for _ in range(n):
        clock()


def _empty_loop(n: int) -> None:
    for _ in range(n):
        pass


def measure_timer_resolution(samples: int) -> Dict[str, Any]:
    """Timer noise floor from back-to-back clock reads."""
    clock = time.perf_counter_ns
    prev = clock()
    deltas: List[int] = []
    for _ in range(samples):
        cur = clock()
        deltas.append(cur - prev)
        prev = cur
    positive = [d for d in deltas if d > 0]
    return {
        "samples": samples,
        "min_positive_delta_ns": min(positive) if positive else None,
        "median_delta_ns": sorted(deltas)[len(deltas) // 2],
        "zero_delta_fraction": 1.0 - len(positive) / len(deltas),
    }


# ==========================================================================
# Reporting helpers
# ==========================================================================
def _fmt_kb(kb: Optional[float]) -> str:
    if kb is None:
        return "unavailable"
    return f"{kb / 1024:g} MB" if kb >= 4096 else f"{kb:g} KB"


def _fmt(v: Any, suffix: str = "") -> str:
    return "unavailable" if v is None else f"{v}{suffix}"


def print_baseline_table(hw: Dict[str, Any]) -> None:
    """Console version of the handout's hardware baseline table."""
    rows = [
        ("CPU Model String", _fmt(hw["cpu_model"]), "L1 Data Cache (per core)", _fmt_kb(hw["l1d_kb"])),
        ("Physical Cores (Pphys)", _fmt(hw["physical_cores"]), "L2 Cache Size", _fmt_kb(hw["l2_kb"])),
        ("Logical Threads (Plog)", _fmt(hw["logical_threads"]), "L3 Cache Size (Shared)",
         "unavailable" if hw["l3_mb"] is None else f"{hw['l3_mb']:g} MB"),
        ("Cache Line Size (Lbytes)", _fmt(hw["cache_line_bytes"], " B"), "Python Version & OS", hw["python_os"]),
    ]
    harness.log(harness.render_table(
        ["Hardware Parameter", "Measured Value", "Hardware Parameter", "Measured Value"], rows))


def resolve_student_id(cli_value: Optional[str]) -> int:
    """Student ID from CLI, env var, previous phase0.json, or harness default; digits only."""
    raw = cli_value or os.environ.get("LAB_STUDENT_ID")
    if not raw:
        prev = harness.load_json("phase0")
        raw = str(prev["student_seed"]) if prev and "student_seed" in prev else harness.DEFAULT_STUDENT_ID
    if not raw or not raw.strip().isdigit():
        raise ValueError("A numeric student ID is required: --student-id <digits>")
    return int(raw.strip())


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Phase 0: hardware audit and timer calibration")
    p.add_argument("--student-id", help="numeric student ID (used as student_seed)")
    p.add_argument("--quick", action="store_true", help="reduced debug mode (hash NOT valid for submission)")
    p.add_argument("--assume-physical", type=int, metavar="N",
                   help="manually supply physical core count if detection fails (recorded as user-supplied)")
    return p.parse_args(argv)


# ==========================================================================
# Main
# ==========================================================================
def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    if args.quick:
        os.environ["LAB_MODE"] = "quick"
    mode = harness.get_mode()
    reads = harness.pick(2_000_000, 200_000)
    try:
        student_seed = resolve_student_id(args.student_id)
    except ValueError as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 2

    harness.log(f"=== PHASE 0: Hardware audit & calibration  (mode={mode}, reads={reads:,}) ===")
    harness.mode_banner()

    harness.log("[1/4] Interrogating hardware ...")
    hw = detect_hardware()
    if args.assume_physical is not None:
        n = args.assume_physical
        if n < 1 or (hw["logical_threads"] and n > hw["logical_threads"]):
            print("[error] --assume-physical must be between 1 and the logical thread count.", file=sys.stderr)
            return 2
        hw["physical_cores"] = n
        hw["sources"]["physical_cores"] = "USER-SUPPLIED (--assume-physical)"
        hw["notes"].append("Physical core count was supplied manually by the student.")
    p_log = mp.cpu_count()
    if hw["logical_threads"] != p_log:
        hw["notes"].append(f"Detected logical threads ({hw['logical_threads']}) != mp.cpu_count() ({p_log}); "
                           "the VERIFY_HASH uses mp.cpu_count().")
        harness.log(f"[warn] {hw['notes'][-1]}")
    if hw["physical_cores"] is None:
        harness.log("[warn] Physical core count NOT detected. Later phases will refuse to run until you "
                    "re-run with --assume-physical N.")

    harness.log("[2/4] Measuring timer noise floor ...")
    resolution = measure_timer_resolution(harness.pick(200_000, 50_000))

    harness.log("[3/4] Measuring perf_counter_ns() call latency ...")
    clock_stats = harness.run_benchmark(lambda: _clock_read_loop(reads), warmup_runs=2, timed_runs=7)
    loop_stats = harness.run_benchmark(lambda: _empty_loop(reads), warmup_runs=2, timed_runs=7)
    avg_latency_ns = clock_stats.median_ms * 1e6 / reads        # raw: includes loop overhead
    loop_overhead_ns = loop_stats.median_ms * 1e6 / reads
    corrected_ns = max(avg_latency_ns - loop_overhead_ns, 0.0)

    harness.log("[4/4] Computing VERIFY_HASH ...")
    metric1 = round(avg_latency_ns, 4)
    metric2 = float(hw["physical_cores"] or 0)
    verify = harness.make_verify_record("PHASE0", student_seed, metric1, metric2, p_log=p_log)

    result = {
        "lab": "Performance Evaluation and Optimization in Parallel Computing",
        "phase": 0, "mode": mode,
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "student_seed": student_seed,
        "hardware": hw,
        "timer": {
            "clock_reads": reads,
            "clock_loop_stats": clock_stats.to_dict(),
            "empty_loop_stats": loop_stats.to_dict(),
            "avg_call_latency_ns": avg_latency_ns,
            "loop_overhead_ns": loop_overhead_ns,
            "loop_corrected_latency_ns": corrected_ns,
            "resolution": resolution,
        },
        "verify": verify,
        "hash_inputs_note": "metric1 = raw avg perf_counter_ns() latency (ns); metric2 = physical cores",
    }
    path = harness.save_json("phase0", result)

    harness.log("\n--- Hardware baseline ---")
    print_baseline_table(hw)
    harness.log("\n--- Timer calibration ---")
    harness.log(f"clock reads                 : {reads:,}")
    harness.log(f"avg call latency (raw)      : {avg_latency_ns:.2f} ns")
    harness.log(f"empty-loop overhead         : {loop_overhead_ns:.2f} ns")
    harness.log(f"loop-corrected latency      : {corrected_ns:.2f} ns")
    harness.log(f"min positive delta (floor)  : {resolution['min_positive_delta_ns']} ns")
    harness.log(f"zero-delta fraction         : {resolution['zero_delta_fraction']:.4f}")
    for note in hw["notes"]:
        harness.log(f"[note] {note}")
    harness.log("\n" + verify["tag"])
    harness.log(f"Saved: {path}")
    return 0


if __name__ == "__main__":
    mp.freeze_support()
    sys.exit(main())
