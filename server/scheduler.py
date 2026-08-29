"""Placement policy over the node registry (v2: jobs are the load).

The policy maps lambda to an ordered ACTIVE node set; the JobManager
places real jobs onto it. Ordering models the paper honestly:
  centrality ON  (AUTO / CEI proper)  -> the shared DOMAIN tier is used LAST
  centrality OFF (fixed-weight strawman, the paper's gamma=0 ablation)
                                      -> the "efficient" DOMAIN tier FIRST

In MOCK mode the per-node thermal intensity is derived from real-time job
occupancy (jobs / PER_NODE_CAP), so spreading fewer jobs per node runs
each node cooler — the duty-cycle physics that real finite processes
produce organically on real GPUs.
"""
import threading

from .jobs import PER_NODE_CAP

LAM_MIN, LAM_MAX = 0.15, 0.90


class Scheduler:
    def __init__(self, registry):
        self.reg = registry
        self.lock = threading.Lock()
        self.running = False
        self.centrality = False          # False = gamma=0 strawman

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
        # policy order preserved (not sorted): placement fills in this order
        return order[:n]

    def apply_thermal(self, jobs_per_node):
        """MOCK thermal model input: occupancy -> intensity."""
        self.reg.set_desired({
            n.id: min(1.0, jobs_per_node.get(n.id, 0) / PER_NODE_CAP)
            for n in self.reg.ordered()})
