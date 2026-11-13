"""
report_generator.py - builds lab_report_<ID>.md from results/*.json (no numbers are invented).

Usage (PowerShell, inside parallel_lab/):
    python report_generator.py
    python report_generator.py --student-id 230103216

Phases without a results file appear as "MEASUREMENTS PENDING". Quick-mode results are flagged
as NOT valid for submission. The appendix embeds the full source of every .py file.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import harness

SOURCE_FILES = ["harness.py", "task0_cal.py", "task1_falsesharing.py", "task2_scaling.py",
                "task3a_roofline.py", "task3b_gemm.py", "task4_scheduling.py", "report_generator.py"]


def md_table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    """GitHub-flavoured Markdown table."""
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    out += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


def f(v: Any, digits: int = 3) -> str:
    """Format a number or 'n/a'."""
    return "n/a" if v is None else f"{v:.{digits}f}"


def pending(name: str) -> str:
    return f"**MEASUREMENTS PENDING** - run the script for {name} (results/{name}.json not found)."


def kb(v: Optional[float]) -> str:
    if v is None:
        return "unavailable"
    return f"{v / 1024:g} MB" if v >= 4096 else f"{v:g} KB"


def section_hardware(p0: Optional[Dict[str, Any]]) -> str:
    if not p0:
        return pending("phase0")
    hw = p0["hardware"]
    l3 = "unavailable" if hw.get("l3_mb") is None else f"{hw['l3_mb']:g} MB"
    rows = [
        ("CPU Model String", hw.get("cpu_model") or "unavailable", "L1 Data Cache (per core)", kb(hw.get("l1d_kb"))),
        ("Physical Cores (Pphys)", hw.get("physical_cores") or "unavailable", "L2 Cache Size", kb(hw.get("l2_kb"))),
        ("Logical Threads (Plog)", hw.get("logical_threads") or "unavailable", "L3 Cache Size (Shared)", l3),
        ("Cache Line Size (Lbytes)", f"{hw.get('cache_line_bytes')} B" if hw.get("cache_line_bytes") else "unavailable",
         "Python Version & OS", hw.get("python_os", "")),
    ]
    text = md_table(["Hardware Parameter", "Measured Value", "Hardware Parameter", "Measured Value"], rows)
    srcs = "; ".join(f"{k}: {v}" for k, v in hw.get("sources", {}).items())
    text += f"\n\nDetection sources: {srcs}\n"
    for note in hw.get("notes", []):
        text += f"\n- Note: {note}"
    return text


def section_phase0(p0: Optional[Dict[str, Any]]) -> str:
    if not p0:
        return pending("phase0")
    t = p0["timer"]
    res = t["resolution"]
    return (f"- Clock reads: {t['clock_reads']:,}\n"
            f"- Average `perf_counter_ns()` call latency (raw): **{t['avg_call_latency_ns']:.2f} ns**\n"
            f"- Empty-loop overhead: {t['loop_overhead_ns']:.2f} ns; loop-corrected latency: {t['loop_corrected_latency_ns']:.2f} ns\n"
            f"- Timer noise floor (minimum positive delta): {res['min_positive_delta_ns']} ns; "
            f"zero-delta fraction {res['zero_delta_fraction']:.4f}\n"
            f"- VERIFY_HASH inputs (choice documented here because the handout leaves them open): "
            f"metric1 = raw average latency in ns, metric2 = physical core count.\n\n"
            f"```\n{p0['verify']['tag']}\npayload: {p0['verify']['payload']}\n```")


def section_phase1(p1: Optional[Dict[str, Any]]) -> str:
    if not p1:
        return pending("phase1")
    prm = p1["parameters"]
    out = [f"Parameters: M = {prm['M']:,} total steps, warm-up {prm['warmup']}, timed runs {prm['timed']}, "
           f"p = P_phys = {prm['P_phys']} for the stride sweep. Time basis: {prm['time_basis']}.\n",
           "### Table 1A - stride sensitivity",
           md_table(["Stride", "Byte separation", "Median ms", "IQR ms", "Mops/s", "Wall median ms (incl. spawn)"],
                    [(r["stride"], r["byte_separation"], f(r["median_ms"], 2), f(r["iqr_ms"], 2),
                      f(r["throughput_mops"], 2), f(r["wall_median_ms"], 2)) for r in p1["table_1a"]])]
    infl = p1.get("inflection", {})
    if infl.get("found"):
        out.append(f"\n**Cache-line boundary identification:** the largest drop occurs between "
                   f"{infl['from_bytes']} B and {infl['to_bytes']} B separation (time x{infl['ratio']:.2f}).")
    else:
        out.append("\n**Cache-line boundary identification:** no precipitous drop was measured on this machine "
                   "(see observations).")
    for key, title in (("stride_1", "stride = 1 (severe false sharing)"), ("stride_16", "stride = 16 (128-byte isolation)")):
        out.append(f"\n### Table 1B - Karp-Flatt series, {title}")
        out.append(md_table(["p", "Median ms", "Speedup Sp", "Efficiency Ep", "Karp-Flatt e(p)"],
                            [(r["p"], f(r["median_ms"], 2), f(r["speedup"], 3), f(r["efficiency"], 3),
                              f(r["karp_flatt"], 4)) for r in p1["table_1b"][key]]))
    out.append("\nKarp-Flatt metric: e(p) = (1/Sp - 1/p) / (1 - 1/p), defined for p >= 2.\n")
    out.append("### Measured observations\n" + "\n".join(f"- {o}" for o in p1["observations"]))
    out.append("""
### Forensic explanation
Cache coherence protocols (MESI/MOESI) track ownership per cache line (64 B on x86-64, 128 B on Apple M-series),
not per word. When several cores write different 8-byte words that live on the same line, every store invalidates
the line in the other cores' caches, so the line ping-pongs between cores although no datum is logically shared:
this is **false sharing**. Stride 8 (8 x 8 B = 64 B) is the first separation that puts neighbouring slots on
different 64-byte lines; stride 16 (128 B) isolates slots on 128-byte-line CPUs and is also safe on 64-byte CPUs
because adjacent-line prefetchers often pair lines. With stride = 1 all workers hit one line, so the useful
(parallel) fraction shrinks and the Karp-Flatt serial fraction e(p) grows with p, because coherence stalls act as
extra serial work that does not shrink with more workers. With stride = 16, e(p) stays low and roughly constant if
the loop scales. Compare the e(p) trajectories in Table 1B above; if they do not differ clearly, the dominant cost
on this machine is the Python interpreter loop (hundreds of ns per iteration) rather than coherence traffic, and
that limitation should be stated rather than hidden.""")
    out.append(f"\n```\n{p1['verify']['tag'] if p1.get('verify') else 'VERIFY_HASH[PHASE1]: not computed'}\n"
               f"payload: {p1['verify']['payload'] if p1.get('verify') else ''}\n```")
    return "\n".join(out)


def section_phase2(p2: Optional[Dict[str, Any]]) -> str:
    if not p2:
        return pending("phase2")
    prm = p2["parameters"]
    out = [f"Parameters: strong N = {prm['N_strong']:,}; weak N0 = {prm['N0_weak']:,}; warm-up {prm['warmup']}, "
           f"timed {prm['timed']}; P_phys = {prm['P_phys']}, P_log = {prm['P_log']}.\n",
           "### Experiment 2A - strong scaling",
           md_table(["p", "T ms", "Speedup", "Efficiency", "Amdahl S(p)", "Stage A ms"],
                    [(r["p"], f(r["median_ms"], 1), f(r["speedup"]), f(r["efficiency"]), f(r["amdahl_speedup"]),
                      f(r["stage_a_ms"], 1)) for r in p2["strong"]]),
           "\n### Experiment 2B - weak scaling",
           md_table(["p", "N(p)", "T ms", "E_weak", "alpha", "S_Gustafson", "Stage A ms"],
                    [(r["p"], f"{r['n']:,}", f(r["median_ms"], 1), f(r["weak_efficiency"]), f(r["alpha"], 4),
                      f(r["gustafson_speedup"]), f(r["stage_a_ms"], 1)) for r in p2["weak"]]),
           f"\n### Amdahl comparison\n- Serial fraction s = T_StageA(1)/T_strong(1) = **{p2['serial_fraction']:.4f}**\n"
           f"- Amdahl asymptote S_inf = 1/s = **{p2['amdahl_s_inf']:.2f}**\n"
           f"- Theoretical speedup at P_log: {p2['amdahl_speedup_at_plog']:.3f}; observed: "
           f"{p2['observed_strong_speedup_at_plog']:.3f}\n",
           "### SMT / core-heterogeneity inflection"]
    m = p2["marginal"]
    if "note" in m:
        out.append(f"- {m['note']}")
    else:
        for key, label in (("mean_marginal_p2_to_pphys", "Mean marginal speedup per worker, p = 2..P_phys"),
                           ("marginal_at_pphys_plus_1", "Marginal speedup of worker P_phys+1"),
                           ("mean_marginal_above_pphys", "Mean marginal speedup above P_phys"),
                           ("drop_vs_mean_below_percent", "Drop of marginal gain at P_phys+1 vs pre-P_phys mean (%)"),
                           ("drop_vs_pphys_step_percent", "Drop vs the last pre-P_phys step (%)")):
            if key in m and m[key] is not None:
                out.append(f"- {label}: {m[key]:.3f}")
    out.append("\n### Measured observations\n" + "\n".join(f"- {o}" for o in p2["observations"]))
    out.append("""
### Interpretation
*Strong scaling* fixes the problem size and asks how much faster p workers finish; Amdahl's law bounds it by the
serial fraction: S(p) = 1 / (s + (1-s)/p), S_inf = 1/s. *Weak scaling* grows the problem with p and asks whether the
time stays constant; Gustafson's scaled speedup is S(p) = p - alpha (p-1), where alpha is the serial share of the
parallel run. Here the serial Stage A covers 5% of N(p), so it grows with p in the weak experiment, which is why
E_weak falls below 1 and alpha is reported per p. Up to P_phys each worker has a private core; beyond it
(Hyper-Threading/SMT or efficiency cores) workers share execution units, so the marginal speedup per added worker
drops - quantified above. Differences between measured and theoretical Amdahl speedup come from pool dispatch cost,
memory/turbo/thermal effects and the SMT sharing, none of which Amdahl's model contains.""")
    v = p2["verify"]
    out.append(f"\n```\n{v['tag']}\npayload: {v['payload']}\n```")
    return "\n".join(out)


def section_phase3(p3a: Optional[Dict[str, Any]], p3b: Optional[Dict[str, Any]]) -> str:
    out = ["### 3A - empirical roofline"]
    if not p3a:
        out.append(pending("phase3a"))
    else:
        bw, comp = p3a["bandwidth"], p3a["compute"]
        out.append(md_table(["Quantity", "Value"], [
            ("Buffers", f"2 x {bw['buffer_bytes'] / 1e6:.0f} MB shared memory, {bw['workers']} workers"),
            ("Median copy time", f"{bw['stats']['median_ms']:.3f} ms (IQR {bw['stats']['iqr_ms']:.3f} ms)"),
            ("BW_sustained", f"**{p3a['bw_sustained_gbs']:.3f} GB/s**"),
            ("FLOP per worker", f"{comp['flop_per_worker']:.3g} ({comp['passes_per_worker']:,} passes x 32 FLOP)"),
            ("Median compute time", f"{comp['stats']['median_ms']:.2f} ms"),
            ("P_sustained", f"**{p3a['p_sustained_gflops']:.5f} GFLOP/s**"),
            ("Ridge point I*", f"**{p3a['ridge_point_flop_per_byte']:.6f} FLOP/Byte**")]))
        out.append("\nLimitations of Python-based FLOP measurement:\n" + "\n".join(f"- {x}" for x in p3a["limitations"]))
        out.append(f"\nSpecification note: {p3a['parameters']['spec_note']}.")
    out.append("\n### 3B - GEMM variants and tile sweep")
    if not p3b:
        out.append(pending("phase3b"))
        return "\n".join(out)
    prm = p3b["parameters"]
    out.append(f"N = {prm['N']}, workers = {prm['workers']}, warm-up {prm['warmup']}, timed {prm['timed']}. "
               f"Numerical validation (< 1e-9): **{'PASSED' if p3b['numerically_validated'] else 'FAILED'}** "
               f"(worst deviation {p3b['worst_error']:.2e}).\n")
    out.append(md_table(["Variant", "BS", "Median ms", "GFLOP/s", "max abs err vs V2", "spot-check err"],
                        [(r["variant"], r["bs"] if r["bs"] else "-", f(r["median_ms"], 1), f(r["gflops"], 5),
                          f"{r['err_vs_v2']:.1e}", f"{r['spot_error']:.1e}") for r in p3b["records"]]))
    out.append(f"\nEmpirical optimum **BS\\* = {p3b['bs_star']}**.\n\n### Tile footprint W_tile = 3 x BS^2 x 8 B")
    out.append(md_table(["BS", "W_tile (bytes)", "Fits L1d", "Fits L2", "I_tiled = BS/12 (FLOP/B)"],
                        [(r["bs"], r["w_tile_bytes"], r["fits_l1"], r["fits_l2"], f(r["intensity_tiled"], 3))
                         for r in p3b["records"] if r["variant"] == 3]))
    out.append("\n### Roofline mapping")
    rl = p3b.get("roofline")
    if rl:
        out.append(f"Measured ceilings: P_sustained = {rl['peak_gflops']:.5f} GFLOP/s, BW = {rl['bw_gbs']:.3f} GB/s, "
                   f"ridge I* = {rl['ridge']:.6f} FLOP/B.\n")
        out.append(md_table(["Variant", "I (FLOP/B)", "Achieved GFLOP/s", "Attainable GFLOP/s", "% of attainable", "Region"],
                            [(r["label"], f(r["intensity"], 3), f(r["achieved_gflops"], 5), f(r["attainable_gflops"], 5),
                              f(r["percent_of_attainable"], 1), r["region"]) for r in rl["rows"]]))
    else:
        out.append("Roofline mapping not available (run task3a_roofline.py in the same mode, then re-run task3b with --force).")
    out.append("\n### Measured observations\n" + "\n".join(f"- {o}" for o in p3b["observations"]))
    out.append("""
### Interpretation
Loop interchange (i-j-k -> i-k-j) turns the stride-N walk over B into unit-stride row traversals and hoists a_ik,
so each fetched cache line is fully used and index arithmetic disappears. Cache blocking additionally keeps a
BS x BS tile of A, B and C resident (W_tile = 3 BS^2 8 B) while it is reused BS times, raising the operational
intensity from about 0.125 FLOP/B (naive) to about BS/12 FLOP/B. The theoretical optimum is the largest BS whose
W_tile fits the private cache level (L1d, or L2 with some loss). In pure CPython the inner loop is dominated by
interpreter dispatch and boxed-float handling and small tiles add slicing overhead, so the measured BS* can lie
above the cache-capacity prediction; compare BS* with the table above and say which effect dominates. Because the
compute ceiling is an interpreter ceiling, the kernels sit far below the memory-bandwidth roof - they are
effectively compute(interpreter)-bound on this machine.""")
    v = p3b["verify"]
    out.append(f"\n```\n{v['tag']}\npayload: {v['payload']}\n```")
    return "\n".join(out)


def section_phase4(p4: Optional[Dict[str, Any]]) -> str:
    if not p4:
        return pending("phase4")
    prm = p4["parameters"]
    out = [f"N = {prm['N']:,} items, {prm['total_iterations']:,} total heavy iterations, workers = {prm['workers']}, "
           f"warm-up {prm['warmup']}, timed {prm['timed']}.\n",
           md_table(["Strategy", "Twall median ms", "IQR ms", "lambda (median)", "lambda (max)", "mean idle ms"],
                    [(r["label"], f(r["twall_median_ms"], 0), f(r["twall_iqr_ms"], 0), f(r["lambda_median"]),
                      f(r["lambda_max"]), f(r["mean_idle_ms"], 0)) for r in p4["rows"]]),
           f"\nOptimal dynamic chunk size on this CPU: **C\\* = {p4['c_star']}**.\n",
           "### Measured observations\n" + "\n".join(f"- {o}" for o in p4["observations"]),
           """
### Interpretation
K(i) grows quadratically, so contiguous static blocks give the last worker most of the work (lambda well above
1.00, other workers idle at the join barrier). Cyclic assignment interleaves cheap and expensive items and
balances almost perfectly for this smooth skew. The dynamic queue balances at run time: with C = 1 the tail skew is
minimal but every item takes the shared counter's lock (cache-line ping-pong and lock hand-off cost); with large C
(e.g. 1024) synchronisation is negligible but the last claimed chunks - which contain the most expensive items -
can leave one worker running long after the rest finish (high lambda, idle time). The best chunk C\\* balances both
effects; because each item here costs thousands of trigonometric iterations, lock overhead is small relative to
the work, so a smaller C is often competitive - compare the measured table above.""",
           f"\n```\n{p4['verify']['tag']}\npayload: {p4['verify']['payload']}\n```"]
    return "\n".join(out)


def section_conclusions(res: Dict[str, Optional[Dict[str, Any]]]) -> str:
    lines = []
    if res["p1"]:
        lines += [f"- Phase 1: {o}" for o in res["p1"]["observations"][:2]]
    if res["p2"]:
        lines += [f"- Phase 2: {o}" for o in res["p2"]["observations"][:2]]
    if res["p3a"]:
        lines.append(f"- Phase 3A: BW = {res['p3a']['bw_sustained_gbs']:.2f} GB/s, interpreter peak = "
                     f"{res['p3a']['p_sustained_gflops']:.4f} GFLOP/s, ridge = {res['p3a']['ridge_point_flop_per_byte']:.5f} FLOP/B.")
    if res["p3b"]:
        lines.append(f"- Phase 3B: {res['p3b']['observations'][0]}")
    if res["p4"]:
        lines += [f"- Phase 4: {o}" for o in res["p4"]["observations"][:2]]
    missing = [k for k, v in res.items() if v is None]
    if missing:
        lines.append(f"- Phases still pending: {', '.join(missing)}.")
    return "\n".join(lines) if lines else "No measurements available yet."


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Generate lab_report_<ID>.md from results/*.json")
    ap.add_argument("--student-id", help="numeric student ID for the file name / header")
    args = ap.parse_args(argv)
    p0 = harness.load_json("phase0")
    res = {"p0": p0, "p1": harness.load_json("phase1"), "p2": harness.load_json("phase2"),
           "p3a": harness.load_json("phase3a"), "p3b": harness.load_json("phase3b"), "p4": harness.load_json("phase4")}
    sid = args.student_id or (str(p0["student_seed"]) if p0 else harness.DEFAULT_STUDENT_ID)
    quick = [k for k, v in res.items() if v and v.get("mode") == "quick"]

    hashes = []
    for tag, key in (("PHASE0", "p0"), ("PHASE1", "p1"), ("PHASE2", "p2"), ("PHASE3", "p3b"), ("PHASE4", "p4")):
        v = res[key]
        hashes.append(v["verify"]["tag"] if v and v.get("verify") else f"VERIFY_HASH[{tag}]: PENDING")

    doc = [f"# Performance Evaluation and Optimization in Parallel Computing - Lab Report",
           f"\n**Student ID:** {sid}  \n**Generated:** {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}  \n"
           f"**Source of all numbers:** results/*.json written by the scripts in this project (nothing typed by hand).\n"]
    if quick:
        doc.append(f"> **WARNING: results from QUICK mode ({', '.join(quick)}) - reduced workload, NOT valid for submission.**\n")
    doc += ["## 1. Hardware Baseline & Verification Hashes", section_hardware(p0),
            "\n### Verification hashes (verbatim)\n```\n" + "\n".join(hashes) + "\n```",
            "\n## 2. Phase 0 - Calibration Telemetry", section_phase0(p0),
            "\n## 3. Phase 1 - False Sharing & Karp-Flatt Analysis", section_phase1(res["p1"]),
            "\n## 4. Phase 2 - Strong vs Weak Scaling", section_phase2(res["p2"]),
            "\n## 5. Phase 3 - Empirical Roofline & GEMM Sweep", section_phase3(res["p3a"], res["p3b"]),
            "\n## 6. Phase 4 - Scheduling & Load Imbalance", section_phase4(res["p4"]),
            "\n## 7. Conclusions", section_conclusions({k: v for k, v in res.items() if k != "p0"}),
            "\n## Appendix A - Complete Python 3 source code"]
    for name in SOURCE_FILES:
        path = harness.PROJECT_DIR / name
        if path.exists():
            doc.append(f"\n### {name}\n\n```python\n{path.read_text(encoding='utf-8')}\n```")
    out_path = harness.PROJECT_DIR / f"lab_report_{sid}.md"
    out_path.write_text("\n".join(doc) + "\n", encoding="utf-8")
    harness.log(f"Report written: {out_path}")
    if quick:
        harness.log("[warn] report contains QUICK-mode results.")
    pend = [k for k, v in res.items() if v is None]
    if pend:
        harness.log(f"[note] pending phases: {', '.join(pend)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
