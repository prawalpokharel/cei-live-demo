#!/usr/bin/env python3
"""Failure-rate x economics frontier — how much resilience is worth buying?

For each (arm, kills-per-trial, seed): run the standard 24-job workload and
inject `kills` transient domain failures at seeded times (fail the declared
domain, revive it 20 s later via /control/revive). Record measured cost per
completed job, lost GPU-seconds, interrupts, makespan, energy.

    python3 scripts/frontier.py --hub https://<pod>-8000.proxy.runpod.net \
        --kills-list 0,1,2,4 --trials 3 --rate 4.40 --out frontier.json

The product question this answers: at what failure rate does risk-aware
placement become economically preferable to aggressive packing?
"""
import argparse
import json
import math
import time

from study import ARMS, call, energy, ci95

FRONTIER_ARMS = ["governed", "fixed-pack", "static-spread"]
REVIVE_AFTER_S = 20


def kill_times_for(seed, kills):
    """Seeded, increasing kill times spread through the active window."""
    ts = []
    for i in range(kills):
        ts.append(40 + i * 50 + (seed * 31 + i * 97) % 21)
    return ts


def run_trial(hub, arm, spec, seed, kills, rate_usd_hr):
    kts = kill_times_for(seed, kills)
    call(hub, "/control/reset", {"seed": seed})
    call(hub, "/control/mode", spec)
    time.sleep(2)
    e0 = energy(call(hub, "/metrics"))
    t0 = time.time()
    call(hub, "/control/start", {})
    for kt in kts:
        time.sleep(max(0, kt - (time.time() - t0)))
        call(hub, "/control/kill_domain", {})
        time.sleep(REVIVE_AFTER_S)
        call(hub, "/control/revive", {})
    deadline = time.time() + 600
    j = None
    while time.time() < deadline:
        j = call(hub, "/metrics")["jobs"]
        if j.get("all_done"):
            break
        time.sleep(5)
    m1 = call(hub, "/metrics")
    wall = time.time() - t0
    j = m1["jobs"]
    cost = wall / 3600 * rate_usd_hr
    done = j["completed"]
    return {
        "arm": arm, "seed": seed, "kills": kills, "kill_times": kts,
        "interrupts": j["interrupted_events"],
        "lost_gpu_s": j["lost_gpu_seconds"],
        "avg_recovery_s": j["avg_recovery_s"],
        "completed": done, "all_done": j.get("all_done", False),
        "energy_wh": round(energy(m1) - e0, 1),
        "wall_s": round(wall, 1),
        "cost_usd": round(cost, 4),
        "usd_per_job": round(cost / done, 5) if done else None,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hub", required=True)
    ap.add_argument("--kills-list", default="0,1,2,4")
    ap.add_argument("--trials", type=int, default=3)
    ap.add_argument("--rate", type=float, default=4.40)
    ap.add_argument("--seed-base", type=int, default=5000)
    ap.add_argument("--out", default="frontier.json")
    args = ap.parse_args()
    hub = args.hub.rstrip("/")
    kill_levels = [int(k) for k in args.kills_list.split(",")]
    seeds = [args.seed_base + t for t in range(args.trials)]

    results = []
    for seed in seeds:                    # paired across arms AND kill levels
        for kills in kill_levels:
            for arm in FRONTIER_ARMS:
                print(f"== seed={seed} kills={kills} arm={arm} "
                      f"(at {kill_times_for(seed, kills)}) ==", flush=True)
                r = run_trial(hub, arm, ARMS[arm], seed, kills, args.rate)
                results.append(r)
                print("   ", {k: r[k] for k in
                              ("interrupts", "lost_gpu_s", "completed",
                               "energy_wh", "wall_s", "usd_per_job")},
                      flush=True)
                with open(args.out, "w") as f:
                    json.dump(results, f, indent=1)

    print("\n===== FRONTIER (mean ± 95% CI per arm x failure count) =====")
    print(f"{'arm':14s} {'kills':>5s} {'$ per job':>16s} {'lost GPU-s':>14s} "
          f"{'interrupts':>12s} {'wall s':>12s} {'Wh':>10s}")
    summary = []
    for arm in FRONTIER_ARMS:
        for kills in kill_levels:
            rs = [r for r in results
                  if r["arm"] == arm and r["kills"] == kills]
            if not rs:
                continue
            mu, cu = ci95([r["usd_per_job"] for r in rs
                           if r["usd_per_job"] is not None])
            ml, cl = ci95([r["lost_gpu_s"] for r in rs])
            mi, ci_ = ci95([r["interrupts"] for r in rs])
            mw, cw = ci95([r["wall_s"] for r in rs])
            me, ce = ci95([r["energy_wh"] for r in rs])
            summary.append({"arm": arm, "kills": kills,
                            "usd_per_job": [mu, cu], "lost_gpu_s": [ml, cl],
                            "interrupts": [mi, ci_], "wall_s": [mw, cw],
                            "energy_wh": [me, ce], "n": len(rs)})
            print(f"{arm:14s} {kills:5d} {mu:9.5f}±{cu:<7.5f} {ml:8.1f}±{cl:<5.1f} "
                  f"{mi:6.1f}±{ci_:<5.1f} {mw:6.1f}±{cw:<5.1f} {me:5.1f}±{ce:<4.1f}")
    with open(args.out, "w") as f:
        json.dump({"trials": results, "summary": summary}, f, indent=1)
    print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
