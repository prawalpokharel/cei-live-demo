"""Node registry — the hub's single source of truth.

Nodes come from two sources and are treated identically:
  - REMOTE: agents/node_agent.py POSTs reports at 1 Hz (rented GPU pods,
    the NVIDIA laptop, Raspberry Pis). The POST reply carries each node's
    desired intensity — the pull-based actuation channel, so nodes never
    need inbound network access.
  - LOCAL MOCK: with MOCK=1 the hub simulates N_GPUS nodes in-process so
    the whole flow rehearses on any laptop for $0.

The DOMAIN (the paper's shared high-centrality tier) is chosen by the
DOMAIN_MATCH env var — any node whose id contains the substring is domain
(e.g. DOMAIN_MATCH=b- puts every GPU on host "b" in the domain). Unset,
the first two nodes in natural order form the domain, matching the
original 6-GPU demo.
"""
import os
import re
import threading
import time

STALE_S = 6.0                       # no report for this long -> offline
DOMAIN_MATCH = os.environ.get("DOMAIN_MATCH", "")


def natkey(s):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", s)]


class Node:
    __slots__ = ("id", "kind", "temp", "watts", "util", "energy_wh",
                 "ts", "failed", "source", "desired", "degraded", "cordoned")

    def __init__(self, nid, kind, source):
        self.id, self.kind, self.source = nid, kind, source
        self.temp = 0.0
        self.watts = 0.0
        self.util = 0.0
        self.energy_wh = 0.0
        self.ts = 0.0
        self.failed = False
        self.desired = 0.0
        self.degraded = False       # exp #6: agent applies a real clock-lock
        self.cordoned = False       # exp #6: excluded from placement, not dead


class Registry:
    def __init__(self):
        self.lock = threading.Lock()
        self.nodes = {}
        self.t0 = time.time()

    # ---- reports (remote agents and the local mock both land here) -------
    def upsert(self, nid, kind, temp, watts, util, source="remote"):
        with self.lock:
            n = self.nodes.get(nid)
            if n is None:
                n = self.nodes[nid] = Node(nid, kind, source)
            now = time.time()
            if n.ts:
                dt = min(now - n.ts, 5.0)
                n.energy_wh += (watts or 0.0) * dt / 3600.0
            n.temp, n.watts, n.util, n.ts = float(temp), float(watts or 0.0), \
                float(util or 0.0), now
            return n

    # ---- views -----------------------------------------------------------
    def ordered(self):
        with self.lock:
            return sorted(self.nodes.values(), key=lambda n: natkey(n.id))

    def fresh(self):
        now = time.time()
        return [n for n in self.ordered() if now - n.ts < STALE_S]

    def healthy(self):
        return [n for n in self.fresh() if not n.failed and not n.cordoned]

    def domain_ids(self):
        nodes = self.ordered()
        if DOMAIN_MATCH:
            return [n.id for n in nodes if DOMAIN_MATCH in n.id]
        return [n.id for n in nodes[:2]]

    # ---- control ---------------------------------------------------------
    def set_desired(self, mapping):
        with self.lock:
            for nid, x in mapping.items():
                if nid in self.nodes:
                    self.nodes[nid].desired = x

    def fail_match(self, match):
        """Fail every node whose id contains `match` (undeclared-domain trials)."""
        ids = [n.id for n in self.ordered() if match in n.id]
        with self.lock:
            for nid in ids:
                self.nodes[nid].failed = True
                self.nodes[nid].desired = 0.0
        return ids

    def degrade_match(self, match, on):
        """Exp #6: mark nodes degraded — the agent applies/reverts a REAL
        GPU clock-lock; the hub only records intent and watches goodput."""
        ids = [n.id for n in self.ordered() if match in n.id]
        with self.lock:
            for nid in ids:
                self.nodes[nid].degraded = bool(on)
        return ids

    def cordon_match(self, match, on):
        """Exp #6: exclude from future placement without killing anything."""
        ids = [n.id for n in self.ordered() if match in n.id]
        with self.lock:
            for nid in ids:
                self.nodes[nid].cordoned = bool(on)
        return ids

    def fail_domain(self):
        ids = self.domain_ids()
        with self.lock:
            for nid in ids:
                if nid in self.nodes:
                    self.nodes[nid].failed = True
                    self.nodes[nid].desired = 0.0
        return ids

    def reset(self):
        with self.lock:
            for n in self.nodes.values():
                n.failed = False
                n.degraded = False
                n.cordoned = False

    def directives_for(self, ids):
        """The actuation payload an agent pulls with each report."""
        with self.lock:
            return {nid: {"intensity": self.nodes[nid].desired,
                          "failed": self.nodes[nid].failed,
                          "degraded": self.nodes[nid].degraded}
                    for nid in ids if nid in self.nodes}

    def snapshot(self):
        now = time.time()
        dom = set(self.domain_ids())
        return {
            "nodes": [{
                "id": n.id, "kind": n.kind,
                "temp": round(n.temp, 1), "watts": round(n.watts, 1),
                "util": round(n.util, 1), "energy_wh": round(n.energy_wh, 2),
                "failed": n.failed, "stale": (now - n.ts) >= STALE_S,
                "degraded": n.degraded, "cordoned": n.cordoned,
                "domain": n.id in dom,
            } for n in self.ordered()],
            "uptime_s": int(now - self.t0),
        }
