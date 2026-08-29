"""Local mock source: with MOCK=1 the hub simulates N_GPUS nodes in-process.

Real hardware never uses this file — real nodes (rented pods, the NVIDIA
laptop, Raspberry Pis) run agents/node_agent.py and report to the hub.
The mock consumes each node's `desired` intensity from the registry —
the same actuation channel remote agents use — so the rehearsal flow and
the real flow are identical from the controller's point of view.

Thermal model: first-order inertia toward a target set by load intensity
(sub-linear, so partial load still warms a card meaningfully) — a
4090-ish demo pacing.
"""
import os
import random

N_GPUS = int(os.environ.get("N_GPUS", "6"))
MOCK = os.environ.get("MOCK", "0") == "1"

_T_IDLE, _T_BURN = 31.0, 80.0
_W_IDLE, _W_BURN = 16.0, 430.0
_ALPHA = 0.12


class LocalMockSource:
    def __init__(self, registry):
        self.reg = registry
        self.temps = {}
        for i in range(N_GPUS):
            nid = f"n{i}"
            self.temps[nid] = _T_IDLE + random.uniform(-1, 1)
            registry.upsert(nid, "mock-gpu", self.temps[nid], _W_IDLE, 0.0,
                            source="local")

    def tick(self):
        for nid, t in self.temps.items():
            node = self.reg.nodes.get(nid)
            x = 0.0 if (node and node.failed) else (node.desired if node else 0)
            target = _T_IDLE + (_T_BURN - _T_IDLE) * (x ** 0.7)
            t += (target - t) * _ALPHA + random.uniform(-0.3, 0.3)
            self.temps[nid] = t
            w = (_W_IDLE + (_W_BURN - _W_IDLE) * x) * random.uniform(0.96, 1.03)
            self.reg.upsert(nid, "mock-gpu", t, w, round(99.0 * x, 1),
                            source="local")
