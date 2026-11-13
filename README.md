# Parallel Computing Lab - Student ID 230103216

Pure Python 3 (standard library only). Windows + PyCharm.

## Files
- `harness.py` - BenchStats, FNV-1a 64, run_benchmark(), emit_verify_tag(), hardware detection (ctypes/winreg), helpers
- `task0_cal.py` - Phase 0: topology + perf_counter_ns latency (1,000,000 calls)
- `task1_falsesharing.py` - Phase 1: false sharing, scaling, Karp-Flatt
- `task2_scaling.py` - Phase 2: strong/weak scaling, Amdahl/Gustafson
- `task3_roofline_gemm.py` - Phase 3: bandwidth, compute ceiling, GEMM x3, tile sweep
- `task4_scheduling.py` - Phase 4: block / cyclic / dynamic scheduling
- `build_report.py` - builds `lab_report_230103216.md` from the REAL results (no numbers are invented)
- `results/` - phaseN_results.txt written by the scripts

## PyCharm setup
1. Unzip, then File > Open > the folder `parallel_computing_230103216`.
2. File > Settings > Project > Python Interpreter: pick any Python 3.10+ (no packages needed).
3. Open the Terminal tab in PyCharm (PowerShell).

## Before the final run (important for stable numbers)
Plug in the charger, set the Windows power mode to Best performance, close browsers/other apps,
do not touch the laptop while a phase runs.

## Quick smoke test (reduced parameters, a few minutes in total) - NOT for submission
```
python task0_cal.py --quick
python task1_falsesharing.py --quick
python task2_scaling.py --quick
python task3_roofline_gemm.py --quick
python task4_scheduling.py --quick
```

## FINAL run (original assignment parameters) - run in this order
```
python task0_cal.py
python task1_falsesharing.py
python task2_scaling.py
python task3_roofline_gemm.py
python task4_scheduling.py
python build_report.py
```
Expected total time: roughly 15-40 minutes depending on the CPU (Phase 4 and Phase 2 are the longest).
Each script prints its `VERIFY_HASH[PHASEn]=...` line and saves results/phaseN_results.txt.
`build_report.py` refuses to run if any result file comes from a `--quick` run.
Do NOT re-run a single phase after building the report without running build_report.py again.

## If physical-core detection fails
Phase 0 prints a warning and uses Pphys = logical count. Find the real value
(Task Manager > Performance > CPU > "Cores", or PowerShell
`Get-CimInstance Win32_Processor | Select NumberOfCores`) and run, e.g. for 6 cores:
```
$env:PPHYS=6
python task1_falsesharing.py
```
(set the same variable before each script, and in the same PowerShell window before build_report.py).

## Common errors
- `BrokenBarrierError` / hang: a worker crashed; look at the first traceback above it. Close other heavy apps and re-run.
- `FileNotFoundError` on shared memory: a previous run was killed; just re-run.
- Windows Firewall/antivirus prompts: allow Python (multiprocessing uses local pipes only).
- Weird/noisy timings: laptop on battery or throttling; see "Before the final run".

## Documented assumptions
The assignment's provided snippets (BenchStats fields, FNV-1a input serialisation, tag format, LCG constants,
exact FMA/trig operation mix) were not available when this project was generated: they are implemented in
the standard way (see the docstrings) and MUST be compared against the handout before submission.
