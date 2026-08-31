#!/usr/bin/env python3
"""iverson what-if — ask a live cluster what a failure would cost, BEFORE it
happens. The command-line twin of the dashboard's what-if panel.

    # rank the fleet by CEI risk (what is fragile right now)
    python3 whatif.py --hub https://<pod>-8000.proxy.runpod.net --list

    # the counterfactual for one node ("if a-g3 fails now, what breaks?")
    python3 whatif.py --hub https://<pod>-8000.proxy.runpod.net a-g3

    # the single worst node right now
    python3 whatif.py --hub https://<pod>-8000.proxy.runpod.net --worst

Reads the hub's /cei (ranked risk) and /whatif?node= (per-node consequence).
Stdlib only. A custom User-Agent is required — the RunPod proxy 403s the
default urllib agent.
"""
import argparse
import json
import sys
import urllib.request

UA = {"User-Agent": "iverson-whatif/1.0"}


def get(hub, path):
    req = urllib.request.Request(hub.rstrip("/") + path, headers=UA)
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def bar(x, width=18):
    n = max(0, min(width, round(x * width)))
    return "█" * n + "·" * (width - n)


def print_ranked(hub, top):
    d = get(hub, "/cei")
    rows = d["ranked"][:top]
    print(f"\nCEI risk ranking — {len(d['ranked'])} nodes "
          f"(setpoint {d.get('setpoint_c')}°C)\n")
    print(f"  {'node':10s} {'CEI':>5s}  {'risk':20s} {'blast':>10s}  action")
    for r in rows:
        action = ("evacuate" if r["cei"] >= 0.6
                  else "watch" if r["cei"] >= 0.35 else "safe to pack")
        blast = f"{r['blast_jobs']}j/{r['blast_gpu_s']:.0f}s"
        print(f"  {r['node']:10s} {r['cei']:5.2f}  {bar(r['cei'])} "
              f"{blast:>10s}  {action}")
    print()


def print_whatif(hub, node):
    w = get(hub, f"/whatif?node={node}")
    if not w.get("known"):
        print(f"\n{node}: not currently scored (offline or idle).\n")
        return
    print(f"\nWHAT-IF · {node} fails right now")
    print(f"  CEI score .............. {w['cei']:.2f}")
    print(f"  jobs interrupted ....... {w['predicted_jobs_interrupted']}")
    print(f"  GPU-seconds lost ....... {w['predicted_gpu_seconds_lost']}")
    print(f"  est. recovery .......... {w['predicted_recovery_s']} s")
    if w.get("in_domain"):
        print("  ⚠ shared high-centrality domain tier")
    if w.get("hosts_dependency"):
        print("  ⚠ hosts a service other jobs depend on")
    print(f"  why: {w.get('rationale', '')}\n")


def main():
    ap = argparse.ArgumentParser(description="ask a live cluster what a "
                                 "failure would cost, before it happens")
    ap.add_argument("node", nargs="?", help="node id, e.g. a-g3")
    ap.add_argument("--hub", required=True, help="hub base URL")
    ap.add_argument("--list", action="store_true", help="rank the fleet by CEI")
    ap.add_argument("--worst", action="store_true",
                    help="what-if for the single highest-CEI node")
    ap.add_argument("--top", type=int, default=10)
    args = ap.parse_args()

    try:
        if args.list:
            print_ranked(args.hub, args.top)
        elif args.worst:
            ranked = get(args.hub, "/cei")["ranked"]
            if not ranked:
                print("no scored nodes yet"); return
            print_whatif(args.hub, ranked[0]["node"])
        elif args.node:
            print_whatif(args.hub, args.node)
        else:
            print_ranked(args.hub, args.top)
    except Exception as e:
        print(f"error talking to hub: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
