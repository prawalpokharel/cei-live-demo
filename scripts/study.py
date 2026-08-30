#!/usr/bin/env python3
"""Randomized-trials study driver — ladder rungs 5–9.

Runs seeded, PAIRED trials of several placement policies against a live
hub: same seed => same job arrivals/durations and same randomized kill
time in every arm. Collects per-trial measured outcomes, then reports
mean ± 95% CI and a cost/energy/goodput table.

    python3 scripts/study.py --hub https://<pod>-8000.proxy.runpod.net \
        --trials 5 --out study_results.json

Arms:
  governed      AUTO (CEI proper)
  fixed-pack    fixed λ=0.90  (the γ=0 pack strawman)
  static-spread fixed λ=0.15  (spread baseline)
  ablation      AUTO with the centrality ordering disabled
"""
import argparse
import json
import math
import time
import urllib.request

UA = {"User-Agent": "cei-study/1.0", "Content-Type": "application/json"}

ARMS = {
    "governed": {"mode": "auto", "lam": 0.85},
    "fixed-pack": {"mode": "fixed", "lam": 0.90},
    "static-spread": {"mode": "fixed", "lam": 0.15},   # ~k8s least-loaded
    "random": {"mode": "fixed", "lam": 0.15, "placement": "random"},
    "ablation": {"mode": "auto", "lam": 0.85, "centrality": False},
}
T95 = {2: 12.71, 3: 4.303, 4: 3.182, 5: 2.776, 6: 2.571, 7: 2.447,
       8: 2.365, 9: 2.306, 10: 2.262}


def call(hub, path, body=None, timeout=15, retries=3):
    for i in range(retries):
        try:
            req = urllib.request.Request(
                hub + path, headers=UA,
                data=(json.dumps(body).encode() if body is not None else None))
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except Exception:
            if i == retries - 1:
                raise
            time.sleep(2)


def energy(m):
    return sum(n["energy_wh"] for n in m["measured"]["nodes"])


def kill_time_for(seed):
    return 60 + (seed * 37) % 61          # deterministic 60–120 s per seed


def run_trial(hub, arm, spec, seed, rate_usd_hr, kill_match=None):
    kill_t = kill_time_for(seed)
    call(hub, "/control/reset", {"seed": seed})
    call(hub, "/control/mode", spec)
    time.sleep(2)
    m0 = call(hub, "/metrics")
    e0 = energy(m0)
    t0 = time.time()
    call(hub, "/control/start", {})
    time.sleep(max(0, kill_t - 3))
    pre = call(hub, "/metrics")["jobs"]          # running-at-kill denominator
    time.sleep(3)
    kill = call(hub, "/control/kill_domain",
                {"match": kill_match} if kill_match else {})
    # drain: wait for all jobs to reach a terminal state (max 300 s)
    deadline = time.time() + 300
    j = None
    while time.time() < deadline:
        m = call(hub, "/metrics")
        j = m["jobs"]
        if j.get("all_done"):
            break
        time.sleep(5)
    m1 = call(hub, "/metrics")
    wall = time.time() - t0
    j = m1["jobs"]
    at_kill = kill.get("jobs_interrupted_now") or 0
    # Same-instant denominator from the kill response (hubs >= v3.1); the
    # T-3s /metrics sample is only a fallback — arrivals in that window can
    # push the fraction above 100%, which is why the same-instant count
    # exists (the v3 study's ">100% interruption rate" artifact).
    run_at_kill = kill.get("jobs_running_now") or pre.get("running") or 0
    return {
        "arm": arm, "seed": seed, "kill_t": kill_t,
        "running_at_kill": run_at_kill,
        "running_denominator": ("same-instant" if kill.get("jobs_running_now")
                                else "T-3s sample"),
        "interrupted_at_kill": at_kill,
        "interrupt_rate_pct": round(100 * at_kill / run_at_kill, 1)
                              if run_at_kill else None,
        "interrupts": j["interrupted_events"],
        "lost_gpu_s": j["lost_gpu_seconds"],
        "avg_recovery_s": j["avg_recovery_s"],
        "submitted": j["submitted"], "completed": j["completed"],
        "all_done": j.get("all_done", False),
        "energy_wh": round(energy(m1) - e0, 1),
        "wall_s": round(wall, 1),
        "cost_usd": round(wall / 3600 * rate_usd_hr, 3),
    }


def ci95(xs):
    n = len(xs)
    if n < 2:
        return (xs[0] if xs else float("nan"), 0.0)
    mean = sum(xs) / n
    sd = math.sqrt(sum((x - mean) ** 2 for x in xs) / (n - 1))
    return (mean, T95.get(n, 1.96) * sd / math.sqrt(n))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hub", required=True)
    ap.add_argument("--trials", type=int, default=5)
    ap.add_argument("--arms", default=",".join(ARMS))
    ap.add_argument("--rate", type=float, default=4.40,
                    help="cluster $/hr for the ROI column")
    ap.add_argument("--kill-match", default=None,
                    help="fail this node-id substring instead of the declared "
                         "domain (undeclared-domain robustness condition)")
    ap.add_argument("--seed-base", type=int, default=1000)
    ap.add_argument("--out", default="study_results.json")
    args = ap.parse_args()
    hub = args.hub.rstrip("/")
    arms = [a for a in args.arms.split(",") if a in ARMS]
    seeds = [args.seed_base + t for t in range(args.trials)]

    results = []
    for seed in seeds:                       # paired: every arm sees each seed
        for arm in arms:
            print(f"== trial seed={seed} arm={arm} "
                  f"(kill at T+{kill_time_for(seed)}s) ==", flush=True)
            r = run_trial(hub, arm, ARMS[arm], seed, args.rate,
                          kill_match=args.kill_match)
            results.append(r)
            print("   ", {k: r[k] for k in
                          ("running_at_kill", "interrupted_at_kill",
                           "interrupt_rate_pct", "interrupts", "lost_gpu_s",
                           "avg_recovery_s", "completed", "energy_wh",
                           "wall_s")}, flush=True)
            with open(args.out, "w") as f:
                json.dump(results, f, indent=1)

    print("\n===== SUMMARY (mean ± 95% CI over paired seeded trials) =====")
    hdr = (f"{'arm':14s} {'int-rate%':>13s} {'interrupts':>14s} {'lost GPU-s':>16s} "
           f"{'recovery s':>12s} {'completed':>12s} {'Wh':>12s} "
           f"{'Wh/job':>8s} {'jobs/$':>7s}")
    print(hdr)
    summary = {}
    for arm in arms:
        rs = [r for r in results if r["arm"] == arm]
        rates = [r["interrupt_rate_pct"] for r in rs
                 if r["interrupt_rate_pct"] is not None]
        mrate, ci_rate = ci95(rates) if rates else (float("nan"), 0)
        mi, ci_i = ci95([r["interrupts"] for r in rs])
        ml, ci_l = ci95([r["lost_gpu_s"] for r in rs])
        rec = [r["avg_recovery_s"] for r in rs if r["avg_recovery_s"] is not None]
        mr, ci_r = ci95(rec) if rec else (float("nan"), 0)
        mc, ci_c = ci95([r["completed"] for r in rs])
        me, ci_e = ci95([r["energy_wh"] for r in rs])
        cost = sum(r["cost_usd"] for r in rs)
        done = sum(r["completed"] for r in rs)
        whpj = (sum(r["energy_wh"] for r in rs) / done) if done else float("nan")
        summary[arm] = {"interrupt_rate_pct": [mrate, ci_rate],
                        "interrupts": [mi, ci_i], "lost_gpu_s": [ml, ci_l],
                        "recovery_s": [mr, ci_r], "completed": [mc, ci_c],
                        "energy_wh": [me, ci_e], "wh_per_job": whpj,
                        "jobs_per_usd": done / cost if cost else None}
        print(f"{arm:14s} {mrate:6.1f}±{ci_rate:<5.1f} {mi:7.1f}±{ci_i:<5.1f} {ml:8.1f}±{ci_l:<6.1f} "
              f"{mr:6.1f}±{ci_r:<4.1f} {mc:6.1f}±{ci_c:<4.1f} "
              f"{me:6.1f}±{ci_e:<4.1f} {whpj:8.2f} {done / cost if cost else 0:7.2f}")
    with open(args.out, "w") as f:
        json.dump({"trials": results, "summary": summary}, f, indent=1)
    print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
