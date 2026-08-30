#!/usr/bin/env python3
"""Experiment #2: CEI DISCOVERS criticality, then it is validated by failure.

Two phases against a live cluster whose GPUs look identical but where some
nodes host a hidden shared service that 2/3 of jobs depend on (the agent
wires this; the hub is never told):

  PHASE 1 — DISCOVERY. With the workload running, briefly PROBE each node
  in turn (SIGSTOP everything on it for a few seconds) and measure the
  cluster-wide drop in aggregate job-progress rate. A node whose suspension
  stalls many other nodes' jobs is inferred high-centrality. This yields a
  discovered criticality score per node WITHOUT being told the graph.

  PHASE 2 — VALIDATION. Rank nodes by discovered score; hard-fail a node
  from the top, middle and bottom of the ranking and measure real damage
  (interrupts + lost GPU-seconds + downstream stalls). If discovered score
  predicts measured damage, CEI is validated as a risk PREDICTOR.

    python3 scripts/discover.py --hub http://... --rate 7.04 --out disc.json
"""
import argparse
import json
import time

from study import call, energy, ci95

PROBE_S = 6
SETTLE_S = 6


def agg_progress_rate(hub, window=4.0):
    """Measured cluster-wide job-progress rate = sum of per-node goodput
    EMAs, averaged over a short window to smooth sampling noise."""
    samples = []
    t_end = time.time() + window
    while time.time() < t_end:
        gp = call(hub, "/metrics").get("goodput", {})
        samples.append(sum(gp.values()))
        time.sleep(1.0)
    return sum(samples) / len(samples) if samples else 0.0


def discover(hub, nodes):
    scores = {}
    base = agg_progress_rate(hub)
    print(f"  baseline aggregate goodput={base:.2f}", flush=True)
    for nid in nodes:
        call(hub, "/control/probe", {"match": nid, "on": True})
        time.sleep(PROBE_S)
        during = agg_progress_rate(hub, window=3.0)
        call(hub, "/control/probe", {"match": nid, "on": False})
        time.sleep(SETTLE_S)
        # criticality = how much suspending THIS node slowed the WHOLE
        # cluster, beyond its own share
        drop = max(0.0, base - during)
        scores[nid] = round(drop, 3)
        print(f"  probe {nid}: agg {base:.2f} -> {during:.2f}  "
              f"score={scores[nid]}", flush=True)
    return scores


def fail_and_measure(hub, nid, seed, rate):
    call(hub, "/control/revive", {})
    call(hub, "/control/reset", {"seed": seed})
    call(hub, "/control/mode", {"mode": "auto", "lam": 0.85})
    time.sleep(2)
    e0 = energy(call(hub, "/metrics"))
    t0 = time.time()
    call(hub, "/control/start", {})
    time.sleep(70)
    kill = call(hub, "/control/kill_domain", {"match": nid})
    deadline = time.time() + 300
    j = None
    while time.time() < deadline:
        j = call(hub, "/metrics")["jobs"]
        if j.get("all_done"):
            break
        time.sleep(5)
    m1 = call(hub, "/metrics")
    j = m1["jobs"]
    return {
        "node": nid, "seed": seed,
        "interrupted_at_kill": kill.get("jobs_interrupted_now", 0),
        "lost_gpu_s": j["lost_gpu_seconds"],
        "completed": j["completed"],
        "wall_s": round(time.time() - t0, 1),
        "energy_wh": round(energy(m1) - e0, 1),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hub", required=True)
    ap.add_argument("--rate", type=float, default=7.04)
    ap.add_argument("--seed-base", type=int, default=6800)
    ap.add_argument("--out", default="exp2_discover.json")
    args = ap.parse_args()
    hub = args.hub.rstrip("/")

    m = call(hub, "/metrics")
    nodes = sorted(n["id"] for n in m["measured"]["nodes"] if not n.get("stale"))
    print(f"discovering criticality over {len(nodes)} nodes", flush=True)

    # start a workload for discovery probing
    call(hub, "/control/reset", {"seed": args.seed_base})
    call(hub, "/control/mode", {"mode": "auto", "lam": 0.85})
    call(hub, "/control/start", {})
    time.sleep(25)                       # let jobs populate and goodput warm
    scores = discover(hub, nodes)
    call(hub, "/control/reset", {"seed": args.seed_base})

    ranked = sorted(nodes, key=lambda n: scores.get(n, 0), reverse=True)
    picks = {"top": ranked[0], "middle": ranked[len(ranked) // 2],
             "bottom": ranked[-1]}
    print(f"ranking (high->low criticality): {ranked}", flush=True)
    print(f"validation picks: {picks}", flush=True)

    validation = []
    for tier, nid in picks.items():
        for t in range(3):
            seed = args.seed_base + 100 + t
            print(f"== validate {tier}={nid} seed={seed} ==", flush=True)
            r = fail_and_measure(hub, nid, seed, args.rate)
            r["tier"] = tier
            r["discovered_score"] = scores.get(nid, 0)
            validation.append(r)
            print("   ", {k: r[k] for k in
                          ("interrupted_at_kill", "lost_gpu_s", "wall_s")},
                  flush=True)
            with open(args.out, "w") as f:
                json.dump({"scores": scores, "ranking": ranked,
                           "picks": picks, "validation": validation}, f,
                          indent=1)

    print("\n===== DISCOVERY VALIDATION =====")
    for tier in ("top", "middle", "bottom"):
        rs = [r for r in validation if r["tier"] == tier]
        mi, ci_ = ci95([r["interrupted_at_kill"] for r in rs])
        ml, cl = ci95([r["lost_gpu_s"] for r in rs])
        sc = rs[0]["discovered_score"]
        print(f"{tier:7s} node={picks[tier]:8s} score={sc:6.2f}  "
              f"interrupted {mi:.1f}±{ci_:.1f}  lost {ml:.1f}±{cl:.1f} GPU-s")
    with open(args.out, "w") as f:
        json.dump({"scores": scores, "ranking": ranked, "picks": picks,
                   "validation": validation}, f, indent=1)
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
