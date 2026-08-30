#!/usr/bin/env python3
"""Experiment #6: cascading degradation before failure — can the hub see a
failure coming and evacuate the right work?

Protocol per seeded trial (paired across the two arms):
  T+0     start the standard workload
  T+45    a REAL degradation begins: the target GPU's clocks are locked
          low (agent runs nvidia-smi -lgc). Jobs there keep running slowly.
  T+150   the degraded GPU hard-fails (SIGKILL of its jobs).
  drain   all jobs complete from checkpoints.

Arms:
  baseline   auto-evacuation OFF — damage lands at the failure
  auto-evac  hub watches measured per-node job-progress rates; when the
             target's goodput sags below threshold it cordons the node and
             gracefully evacuates (loss bounded by checkpoint age)

Recorded per trial: detection lead time (seconds before the hard failure
that the hub cordoned the node), jobs evacuated + their bounded loss,
jobs interrupted AT the failure + loss, completions, energy, wall.

    python3 scripts/predictfail.py --hub http://... --target -g4 \
        --trials 3 --rate 7.92 --out exp6_predictfail.json
"""
import argparse
import json
import time

from study import call, energy, ci95

DEGRADE_T = 45
KILL_T = 150


def run_trial(hub, arm, seed, target, rate):
    call(hub, "/control/degrade", {"match": target, "on": False})
    call(hub, "/control/revive", {})
    call(hub, "/control/reset", {"seed": seed})
    call(hub, "/control/mode", {"mode": "auto", "lam": 0.85})
    call(hub, "/control/auto_evacuate",
         {"on": arm == "auto-evac", "threshold": 0.55})
    time.sleep(2)
    e0 = energy(call(hub, "/metrics"))
    t0 = time.time()
    call(hub, "/control/start", {})

    t_detect = None
    lost_before_kill = 0.0
    evac_before_kill = 0

    def wait_until(t_rel):
        nonlocal t_detect
        while time.time() - t0 < t_rel:
            m = call(hub, "/metrics")
            if t_detect is None:
                for n in m["measured"]["nodes"]:
                    if target in n["id"] and n.get("cordoned"):
                        t_detect = round(time.time() - t0, 1)
            time.sleep(1.0)

    wait_until(DEGRADE_T)
    call(hub, "/control/degrade", {"match": target, "on": True})
    wait_until(KILL_T)
    pre = call(hub, "/metrics")["jobs"]
    lost_before_kill = pre["lost_gpu_seconds"]
    evac_before_kill = pre.get("evac_events", 0)
    kill = call(hub, "/control/kill_domain", {"match": target})
    call(hub, "/control/degrade", {"match": target, "on": False})

    deadline = time.time() + 420
    j = None
    while time.time() < deadline:
        j = call(hub, "/metrics")["jobs"]
        if j.get("all_done"):
            break
        time.sleep(5)
    m1 = call(hub, "/metrics")
    j = m1["jobs"]
    wall = time.time() - t0
    return {
        "arm": arm, "seed": seed, "target": target,
        "degrade_t": DEGRADE_T, "kill_t": KILL_T,
        "t_detect": t_detect,
        "lead_time_s": round(KILL_T - t_detect, 1) if t_detect else None,
        "evacuated_jobs": j.get("evac_events", 0),
        "evac_lost_gpu_s": j.get("evac_lost_gpu_s", 0.0),
        "interrupted_at_kill": kill.get("jobs_interrupted_now", 0),
        "lost_at_kill_gpu_s": round(j["lost_gpu_seconds"]
                                    - lost_before_kill, 1),
        "lost_total_gpu_s": j["lost_gpu_seconds"],
        "completed": j["completed"], "all_done": j.get("all_done", False),
        "energy_wh": round(energy(m1) - e0, 1),
        "wall_s": round(wall, 1),
        "cost_usd": round(wall / 3600 * rate, 4),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hub", required=True)
    ap.add_argument("--target", default="-g4")
    ap.add_argument("--trials", type=int, default=3)
    ap.add_argument("--rate", type=float, default=7.92)
    ap.add_argument("--seed-base", type=int, default=6600)
    ap.add_argument("--out", default="exp6_predictfail.json")
    args = ap.parse_args()
    hub = args.hub.rstrip("/")

    results = []
    for t in range(args.trials):
        seed = args.seed_base + t
        for arm in ("baseline", "auto-evac"):
            print(f"== trial seed={seed} arm={arm} target={args.target} ==",
                  flush=True)
            r = run_trial(hub, arm, seed, args.target, args.rate)
            results.append(r)
            print("   ", {k: r[k] for k in
                          ("t_detect", "lead_time_s", "evacuated_jobs",
                           "evac_lost_gpu_s", "interrupted_at_kill",
                           "lost_at_kill_gpu_s", "completed", "wall_s")},
                  flush=True)
            with open(args.out, "w") as f:
                json.dump(results, f, indent=1)

    print("\n===== PREDICT-BEFORE-FAIL SUMMARY =====")
    for arm in ("baseline", "auto-evac"):
        rs = [r for r in results if r["arm"] == arm]
        mk, ck = ci95([r["lost_at_kill_gpu_s"] for r in rs])
        mi, ci_ = ci95([r["interrupted_at_kill"] for r in rs])
        leads = [r["lead_time_s"] for r in rs if r["lead_time_s"]]
        print(f"{arm:10s} interrupted@kill {mi:.1f}±{ci_:.1f}  "
              f"lost@kill {mk:.1f}±{ck:.1f} GPU-s  "
              f"lead {sum(leads)/len(leads):.0f}s" if leads else
              f"{arm:10s} interrupted@kill {mi:.1f}±{ci_:.1f}  "
              f"lost@kill {mk:.1f}±{ck:.1f} GPU-s  lead —")
    with open(args.out, "w") as f:
        json.dump({"trials": results}, f, indent=1)
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
