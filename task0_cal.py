"""Phase 0 - hardware topology and telemetry (Student ID 230103216)."""
import multiprocessing
import time

from harness import (QUICK, detect_topology, emit_verify_tag, save_results,
                     topology_table, fmt_bytes)


def measure_timer_latency(n: int = 1_000_000):
    pc = time.perf_counter_ns
    for _ in range(10000):  # warm-up
        pc()
    t0 = pc()
    for _ in range(n):
        pc()
    t1 = pc()
    gross = (t1 - t0) / n
    t0 = pc()
    for _ in range(n):
        pass
    t1 = pc()
    loop = (t1 - t0) / n
    return gross, loop


def main():
    topo = detect_topology()
    gross, loop = measure_timer_latency(1_000_000)  # exactly 1,000,000 calls
    net = max(gross - loop, 0.0)

    body = ["## Hardware telemetry", "", topology_table(topo), ""]
    body.append(f"Pphys used by all later phases: **{topo['Pphys']}**; P_log: **{topo['P_log']}**.")
    if topo["efficiency_classes"]:
        body.append(f"Windows efficiency classes reported: {topo['efficiency_classes']}.")
    body.append("")
    body.append("Cache detail as reported by the OS (level/type -> sizes found on this CPU):")
    for k, v in sorted(topo["cache_detail"].items()):
        body.append(f"- {k}: {', '.join(fmt_bytes(x) for x in v)}")
    if not topo["cache_detail"]:
        body.append("- unavailable")
    body += ["", "Notes:"] + [f"- {n}" for n in topo["notes"]]
    unavailable = [n for n, v in (("L1d", topo["l1d"]), ("L2", topo["l2"]), ("L3", topo["l3"]),
                                  ("cache line", topo["line_size"]), ("physical cores", topo["physical"]),
                                  ("CPU model", topo["cpu_model"])) if v is None]
    if unavailable:
        body += ["", "**Unavailable through standard interfaces: " + ", ".join(unavailable) + ".**",
                 "Reproducible alternative (PowerShell): `Get-CimInstance Win32_Processor | "
                 "Select Name,NumberOfCores,NumberOfLogicalProcessors,L2CacheSize,L3CacheSize`, or Sysinternals "
                 "`coreinfo -c -l` (cache sizes/line size). Then re-run with the environment variable PPHYS set."]
    body += ["", "## perf_counter_ns() latency (1,000,000 consecutive calls)", "",
             f"- Gross average per call (loop included): {gross:.2f} ns",
             f"- Empty-loop overhead per iteration: {loop:.2f} ns",
             f"- Net estimated call latency: {net:.2f} ns",
             "- Timer resolution is therefore adequate for all measurements below (all >> 1 us)."]
    print("\n".join(body))
    tag = emit_verify_tag(0, [float(topo["Pphys"]), float(topo["P_log"]), gross])
    save_results(0, body, tag)


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
