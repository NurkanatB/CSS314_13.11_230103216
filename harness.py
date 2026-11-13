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
