"""Scheduler shim + synthetic job stream, over the node registry.

The shim is the controller's ACTUATOR: each epoch it maps the current lambda
to a set of active nodes and writes each node's desired intensity into the
registry. Local mock nodes consume it in-process; remote agents pull it in
their 1 Hz report reply and reconcile real burn processes.

Ordering models the paper honestly:
  centrality ON  (AUTO / CEI proper)  -> the shared DOMAIN tier is used LAST
  centrality OFF (fixed-weight strawman, the paper's gamma=0 ablation)
                                      -> the "efficient" DOMAIN tier FIRST

MODELED zone: the job stream is synthetic (arrivals, durations, placement,
jobs-lost). Placement follows the MEASURED state; the tally is a model —
the dashboard labels it that way and you say it aloud once.
"""
import random
import threading

LAM_MIN, LAM_MAX = 0.15, 0.90


class Scheduler:
    def __init__(self, registry):
        self.reg = registry
        self.lock = threading.Lock()
        self.jobs = []                   # [{node, remaining_s}]
        self.jobs_done = 0
        self.jobs_lost = 0
        self.running = False
        self.centrality = False          # False = gamma=0 strawman

    # ---- lambda -> active node set (the actuator) ------------------------
    def active_set(self, lam):
        healthy = [n.id for n in self.reg.healthy()]
        if not healthy:
            return []
        N = len(healthy)
        frac = (lam - LAM_MIN) / (LAM_MAX - LAM_MIN)     # 0=spread .. 1=pack
        n = max(2, min(N, round(N - frac * (N - 2))))
        dom = [i for i in self.reg.domain_ids() if i in healthy]
        rest = [i for i in healthy if i not in dom]
        order = (rest + dom) if self.centrality else (dom + rest)
        return sorted(order[:n])

    def apply(self, lam):
        if not self.running:
            self.reg.set_desired({n.id: 0.0 for n in self.reg.ordered()})
            return
        active = set(self.active_set(lam))
        # one job stream shared by n nodes -> per-node intensity drops.
        # (Real agents treat intensity as a burn on/off + duty knob.)
        inten = min(1.0, 2.2 / max(1, len(active)))
        self.reg.set_desired({n.id: (inten if n.id in active else 0.0)
                              for n in self.reg.ordered()})

    # ---- synthetic job stream (MODELED) ---------------------------------
    def step_jobs(self, lam, dt):
        with self.lock:
            active = self.active_set(lam)
            healthy = [n.id for n in self.reg.healthy()]
            if active and random.random() < 0.55:
                # As in the paper: under pressure a few SHORT jobs still land
                # on the domain tier; the standing population there stays small.
                if healthy and random.random() < 0.15:
                    node, dur = random.choice(healthy), random.uniform(10, 25)
                else:
                    node, dur = random.choice(active), random.uniform(25, 90)
                self.jobs.append({"node": node, "remaining_s": dur})
            for j in self.jobs[:]:
                j["remaining_s"] -= dt
                if j["remaining_s"] <= 0:
                    self.jobs.remove(j)
                    self.jobs_done += 1

    # ---- the red-button moment ------------------------------------------
    def kill_domain(self):
        """Take down the workload on the shared domain. Honest narration:
        this kills the WORKLOAD on a real shared domain (and, on the
        physical rig, the wired relay cuts its actual power feed)."""
        ids = set(self.reg.fail_domain())
        with self.lock:
            lost = [j for j in self.jobs if j["node"] in ids]
            self.jobs_lost += len(lost)
            for j in lost:
                self.jobs.remove(j)
            return len(lost)

    def reset(self):
        self.reg.reset()
        with self.lock:
            self.jobs, self.jobs_done, self.jobs_lost = [], 0, 0

    def snapshot(self):
        with self.lock:
            per_node = {}
            for j in self.jobs:
                per_node[j["node"]] = per_node.get(j["node"], 0) + 1
            return {"running": self.running, "jobs": len(self.jobs),
                    "jobs_per_node": per_node, "jobs_done": self.jobs_done,
                    "jobs_lost": self.jobs_lost,
                    "domain": self.reg.domain_ids(),
                    "centrality": self.centrality}
