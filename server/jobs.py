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
        self.rng = random.Random()
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
        if self.rng.random() < ARRIVAL_P:
            self.seq += 1
            jid = f"j{self.seq}"
            self.jobs[jid] = {
                "id": jid, "state": "queued", "node": None,
                "duration_s": round(self.rng.uniform(JOB_MIN_S, JOB_MAX_S), 1),
                "progress_s": 0.0, "ckpt_s": 0.0, "attempt": 1,
                "submitted": time.time(), "started": None,
                "interrupted_at": None,
            }

    # ---- placement over the policy's active set --------------------------
    def assign(self, active_ids, random_place=False):
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
                if random_place:
                    open_ids = [n for n in active_ids if loads[n] < PER_NODE_CAP]
                    if not open_ids:
                        continue
                    nid = self.rng.choice(open_ids)
                else:
                    nid = min(active_ids,
                              key=lambda n: (loads[n], active_ids.index(n)))
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
    def agent_status(self, host, statuses):
        """statuses: [{id, elapsed, done, alive}] from one node agent.
        Guard: only the agent whose node currently OWNS a job may update it —
        a dead process report from a failed node must not re-interrupt a job
        already requeued to a survivor (learned in ARM-1, 2026-08-29)."""
        with self.lock:
            for st in statuses:
                j = self.jobs.get(st["id"])
                if not j or j["state"] != "running":
                    continue
                if not j["node"] or not j["node"].startswith(host + "-"):
                    continue
                j["progress_s"] = max(j["progress_s"], float(st.get("elapsed", 0)))
                j["ckpt_s"] = max(j["ckpt_s"], float(st.get("ckpt", 0)))
                j["ckpt_writes"] = max(j.get("ckpt_writes", 0),
                                       int(st.get("ckpt_writes", 0)))
                j["ckpt_cost_s"] = max(j.get("ckpt_cost_s", 0.0),
                                       float(st.get("ckpt_cost_s", 0.0)))
                if st.get("done"):
                    j["state"] = "completed"
                elif not st.get("alive", True):
                    self._interrupt(j)     # process died without completing

    def _interrupt(self, j):
        import sys
        print(f"INTERRUPT {j['id']} node={j['node']} prog={j['progress_s']:.1f} "
              f"ckpt={j['ckpt_s']:.1f} attempt={j['attempt']}", file=sys.stderr, flush=True)
        self.interrupted_events += 1
        # With checkpointing, only work since the last durable checkpoint is
        # lost; the job resumes from ckpt on a survivor (real migration).
        self.lost_gpu_s += max(0.0, j["progress_s"] - j["ckpt_s"])
        j["state"] = "queued"
        j["attempt"] += 1
        j["progress_s"] = j["ckpt_s"]
        j["interrupted_at"] = time.time()
        j["node"] = None

    # ---- goodput tracking (experiment #6) --------------------------------
    # EMA of measured job-progress rate per node (1.0 = full speed). A GPU
    # that is clock-locked keeps its jobs alive but their heartbeat progress
    # slows — this is the signal that catches "slow, not dead".
    def node_goodput(self):
        with self.lock:
            now = time.time()
            rates = {}
            for j in self.jobs.values():
                if j["state"] != "running" or not j["node"]:
                    continue
                last = j.get("_gp_last")            # (ts, progress)
                if last:
                    dt = now - last[0]
                    if dt >= 2.0:
                        rate = (j["progress_s"] - last[1]) / dt
                        rates.setdefault(j["node"], []).append(rate)
                        j["_gp_last"] = (now, j["progress_s"])
                else:
                    j["_gp_last"] = (now, j["progress_s"])
            out = {}
            for nid, rs in rates.items():
                ema = self._gp_ema.get(nid, 1.0)
                inst = sum(rs) / len(rs)
                self._gp_ema[nid] = ema * 0.7 + inst * 0.3
            return dict(self._gp_ema)

    _gp_ema = {}

    evac_events = 0
    evac_lost_gpu_s = 0.0

    def evacuate_nodes(self, node_ids):
        """Graceful migration: requeue running jobs from these nodes. The
        loss per job is bounded by work since its last real checkpoint —
        the whole point of evacuating BEFORE the hard failure."""
        with self.lock:
            moved = 0
            lost0 = self.lost_gpu_s
            for j in self.jobs.values():
                if j["state"] == "running" and j["node"] in node_ids:
                    self._interrupt(j)
                    moved += 1
            self.evac_events += moved
            self.evac_lost_gpu_s += self.lost_gpu_s - lost0
            return moved

    # ---- node failure (the red button) -----------------------------------
    def fail_nodes(self, node_ids):
        """Returns (interrupted_now, running_at_kill) counted at the SAME
        instant, so interruption fraction has a same-instant denominator —
        a denominator sampled even seconds earlier can be exceeded when
        jobs arrive in the window (the v3 study's >100% artifact)."""
        with self.lock:
            hit = 0
            running = 0
            for j in self.jobs.values():
                if j["state"] != "running":
                    continue
                running += 1
                if j["node"] in node_ids:
                    self._interrupt(j)
                    hit += 1
            return hit, running

    # ---- checkpoint policy (experiment #7) -------------------------------
    # "default"    -> jobs use their own CKPT_EVERY_S default (10 s)
    # "fixed:N"    -> every job checkpoints every N s
    # "adaptive"   -> jobs on declared-domain (fragile) nodes checkpoint
    #                 every 5 s, all others every 45 s — checkpoint effort
    #                 follows declared criticality
    ckpt_policy = "default"
    domain_ids = frozenset()          # app.py refreshes from the registry

    def ckpt_interval_for(self, nid):
        p = self.ckpt_policy
        if p.startswith("fixed:"):
            return float(p.split(":", 1)[1])
        if p == "adaptive":
            return 5.0 if nid in self.domain_ids else 45.0
        return None

    # ---- directives for one agent ----------------------------------------
    def jobs_for(self, node_ids):
        with self.lock:
            out = {}
            for nid in node_ids:
                ck = self.ckpt_interval_for(nid)
                out[nid] = [{"id": j["id"], "seconds": j["duration_s"],
                             "resume_s": j["ckpt_s"],
                             **({"ckpt_every_s": ck} if ck else {})}
                            for j in self.jobs.values()
                            if j["state"] == "running" and j["node"] == nid]
            return out

    def running_detail(self):
        """{node_id: [{progress_s, ckpt_s}]} for CEI blast-radius scoring."""
        with self.lock:
            out = {}
            for j in self.jobs.values():
                if j["state"] == "running" and j["node"]:
                    out.setdefault(j["node"], []).append(
                        {"progress_s": j["progress_s"], "ckpt_s": j["ckpt_s"]})
            return out

    def reset(self, seed=None):
        with self.lock:
            if seed is not None:
                self.rng.seed(int(seed))
            self.jobs.clear()
            self.seq = 0
            self.arrivals_done = False
            self.recoveries = []
            self.lost_gpu_s = 0.0
            self.interrupted_events = 0
            self.evac_events = 0
            self.evac_lost_gpu_s = 0.0
            self._gp_ema = {}

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
                "ckpt_policy": self.ckpt_policy,
                "ckpt_writes": sum(j.get("ckpt_writes", 0)
                                   for j in self.jobs.values()),
                "ckpt_cost_s": round(sum(j.get("ckpt_cost_s", 0.0)
                                         for j in self.jobs.values()), 2),
                "evac_events": self.evac_events,
                "evac_lost_gpu_s": round(self.evac_lost_gpu_s, 1),
                "avg_recovery_s": round(sum(self.recoveries) /
                                        len(self.recoveries), 1)
                                  if self.recoveries else None,
                "jobs_per_node": per_node,
                "arrivals_done": self.arrivals_done,
                "all_done": self.arrivals_done and
                            by("completed") == self.seq and self.seq > 0,
            }
