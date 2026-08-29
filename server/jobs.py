"""JobManager — real job accounting over the node registry.

v2 of the demo's workload layer. The ARRIVAL SCHEDULE is synthetic (a
scripted Poisson stream — say so on stage), but everything after arrival
is REAL in real mode: each job is an OS process on a GPU, spawned by the
node agent, with heartbeat progress; an interrupt is a killed process,
lost GPU-seconds are the heartbeat's last elapsed value, and recovery
time is the measured gap between interruption and restart on a survivor.

In MOCK mode the same accounting runs with hub-simulated progress, so
the $0 rehearsal exercises identical code paths.

States: queued -> running -> completed | interrupted -> queued (requeue)
"""
import os
import random
import threading
import time

ARRIVAL_P = float(os.environ.get("ARRIVAL_P", "0.30"))     # jobs/sec prob
TOTAL_JOBS = int(os.environ.get("TOTAL_JOBS", "40"))       # bounded experiment
JOB_MIN_S = float(os.environ.get("JOB_MIN_S", "30"))
JOB_MAX_S = float(os.environ.get("JOB_MAX_S", "90"))
PER_NODE_CAP = int(os.environ.get("PER_NODE_CAP", "4"))


class JobManager:
    def __init__(self, registry, mock):
        self.reg = registry
        self.mock = mock
        self.lock = threading.Lock()
        self.jobs = {}                 # id -> dict
        self.seq = 0
        self.arrivals_done = False
        self.recoveries = []           # measured interrupt->restart gaps (s)
        self.lost_gpu_s = 0.0
        self.interrupted_events = 0

    # ---- arrivals (synthetic schedule; real execution) -------------------
    def _arrive(self):
        if self.seq >= TOTAL_JOBS:
            self.arrivals_done = True
            return
        if random.random() < ARRIVAL_P:
            self.seq += 1
            jid = f"j{self.seq}"
            self.jobs[jid] = {
                "id": jid, "state": "queued", "node": None,
                "duration_s": round(random.uniform(JOB_MIN_S, JOB_MAX_S), 1),
                "progress_s": 0.0, "attempt": 1,
                "submitted": time.time(), "started": None,
                "interrupted_at": None,
            }

    # ---- placement over the policy's active set --------------------------
    def assign(self, active_ids):
        with self.lock:
            if not active_ids:
                return
            loads = {n: 0 for n in active_ids}
            for j in self.jobs.values():
                if j["state"] == "running" and j["node"] in loads:
                    loads[j["node"]] += 1
            for j in self.jobs.values():
                if j["state"] != "queued":
                    continue
                nid = min(active_ids, key=lambda n: (loads[n], active_ids.index(n)))
                if loads[nid] >= PER_NODE_CAP:
                    continue
                j["node"] = nid
                j["state"] = "running"
                j["started"] = time.time()
                if j["interrupted_at"] is not None:
                    self.recoveries.append(time.time() - j["interrupted_at"])
                    j["interrupted_at"] = None
                loads[nid] += 1

    # ---- per-second tick -------------------------------------------------
    def tick(self, running):
        with self.lock:
            if running and not self.arrivals_done:
                self._arrive()
            if self.mock:
                for j in self.jobs.values():
                    if j["state"] == "running":
                        j["progress_s"] += 1.0
                        if j["progress_s"] >= j["duration_s"]:
                            j["state"] = "completed"

    # ---- agent reports (real mode) ---------------------------------------
    def agent_status(self, statuses):
        """statuses: [{id, elapsed, done, alive}] from a node agent."""
        with self.lock:
            for st in statuses:
                j = self.jobs.get(st["id"])
                if not j or j["state"] != "running":
                    continue
                j["progress_s"] = max(j["progress_s"], float(st.get("elapsed", 0)))
                if st.get("done"):
                    j["state"] = "completed"
                elif not st.get("alive", True):
                    self._interrupt(j)     # process died without completing

    def _interrupt(self, j):
        self.interrupted_events += 1
        self.lost_gpu_s += j["progress_s"]
        j["state"] = "queued"              # requeue for recovery measurement
        j["attempt"] += 1
        j["progress_s"] = 0.0
        j["interrupted_at"] = time.time()
        j["node"] = None

    # ---- node failure (the red button) -----------------------------------
    def fail_nodes(self, node_ids):
        with self.lock:
            hit = 0
            for j in self.jobs.values():
                if j["state"] == "running" and j["node"] in node_ids:
                    self._interrupt(j)
                    hit += 1
            return hit

    # ---- directives for one agent ----------------------------------------
    def jobs_for(self, node_ids):
        with self.lock:
            return {nid: [{"id": j["id"], "seconds": j["duration_s"]}
                          for j in self.jobs.values()
                          if j["state"] == "running" and j["node"] == nid]
                    for nid in node_ids}

    def reset(self):
        with self.lock:
            self.jobs.clear()
            self.seq = 0
            self.arrivals_done = False
            self.recoveries = []
            self.lost_gpu_s = 0.0
            self.interrupted_events = 0

    def snapshot(self):
        with self.lock:
            by = lambda s: sum(1 for j in self.jobs.values() if j["state"] == s)
            per_node = {}
            for j in self.jobs.values():
                if j["state"] == "running":
                    per_node[j["node"]] = per_node.get(j["node"], 0) + 1
            return {
                "mode": "mock-simulated" if self.mock else "real-processes",
                "arrival_note": "synthetic schedule, real execution"
                                if not self.mock else "synthetic schedule + simulated execution",
                "submitted": self.seq, "total_planned": TOTAL_JOBS,
                "queued": by("queued"), "running": by("running"),
                "completed": by("completed"),
                "interrupted_events": self.interrupted_events,
                "lost_gpu_seconds": round(self.lost_gpu_s, 1),
                "avg_recovery_s": round(sum(self.recoveries) /
                                        len(self.recoveries), 1)
                                  if self.recoveries else None,
                "jobs_per_node": per_node,
                "arrivals_done": self.arrivals_done,
            }
