#!/usr/bin/env python3
"""Universal node agent v2 — telemetry up, REAL jobs down. Stdlib only.

    HUB=https://<podid>-8000.proxy.runpod.net HOST=a python3 node_agent.py
    HUB=http://localhost:8123 HOST=b MOCK_GPUS=2 ALLOW_CPU=1 python3 node_agent.py

Each 1 Hz report carries real telemetry (nvidia-smi / vcgencmd / mock) and
the status of every job process this agent runs. The reply's directives
assign REAL jobs per node: the agent spawns one OS process per job
(jobs/gpu_job.py, fetched from the hub's /jobscript), pinned to the
node's GPU. A failed node directive SIGKILLs its jobs — a real interrupt,
whose lost progress the hub reads from the job's last heartbeat.

ALLOW_CPU=1 lets jobs run on CPU (validation on GPU-less machines).
MEASURED discipline: telemetry is only what a sensor said; job progress
is only what the job process itself wrote.
"""
import json
import os
import random
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request

HUB = os.environ.get("HUB", "http://localhost:8000").rstrip("/")
HOST = os.environ.get("HOST", socket.gethostname().split(".")[0])
MOCK_GPUS = int(os.environ.get("MOCK_GPUS", "0"))
ALLOW_CPU = os.environ.get("ALLOW_CPU", "0") == "1"
PDIR = os.environ.get("PROGRESS_DIR", "/tmp/cei-jobs-" + HOST)
JOB_SCRIPT = os.environ.get("JOB_SCRIPT", "/tmp/cei_gpu_job.py")

_T_IDLE, _T_BURN = 31.0, 80.0
_W_IDLE, _W_BURN = 16.0, 430.0
_ALPHA = 0.12


def detect():
    if MOCK_GPUS > 0:
        return "mock"
    if shutil.which("nvidia-smi"):
        return "nvidia"
    if shutil.which("vcgencmd"):
        return "pi"
    print("!! no nvidia-smi or vcgencmd; set MOCK_GPUS=n to simulate telemetry")
    sys.exit(1)


def read_nvidia():
    out = subprocess.run(
        ["nvidia-smi",
         "--query-gpu=index,power.draw,temperature.gpu,utilization.gpu",
         "--format=csv,noheader,nounits"],
        capture_output=True, text=True, timeout=5).stdout
    nodes = []
    for line in out.strip().splitlines():
        idx, p, t, u = [x.strip() for x in line.split(",")]
        watts = 0.0 if p in ("N/A", "[N/A]") else float(p)
        nodes.append({"id": f"{HOST}-g{idx}", "kind": "gpu",
                      "temp": float(t), "watts": watts, "util": float(u)})
    return nodes


def read_pi():
    out = subprocess.run(["vcgencmd", "measure_temp"],
                         capture_output=True, text=True, timeout=5).stdout
    t = float(out.split("=")[1].split("'")[0])
    return [{"id": f"{HOST}-soc", "kind": "pi", "temp": t,
             "watts": 0.0, "util": 0.0}]


class MockBank:
    """Mock telemetry whose temperature follows this node's REAL job load."""
    def __init__(self, k, runner):
        self.runner = runner
        self.temps = {f"{HOST}-g{i}": _T_IDLE + random.uniform(-1, 1)
                      for i in range(k)}

    def read(self):
        nodes = []
        per = self.runner.per_node_count()
        for nid, t in self.temps.items():
            x = min(1.0, per.get(nid, 0) / 4.0)
            target = _T_IDLE + (_T_BURN - _T_IDLE) * (x ** 0.7)
            t += (target - t) * _ALPHA + random.uniform(-0.3, 0.3)
            self.temps[nid] = t
            w = (_W_IDLE + (_W_BURN - _W_IDLE) * x) * random.uniform(0.96, 1.03)
            nodes.append({"id": nid, "kind": "mock-gpu", "temp": round(t, 1),
                          "watts": round(w, 1), "util": round(99.0 * x, 1)})
        return nodes


class JobRunner:
    """One real OS process per job, pinned to the node's device."""
    def __init__(self):
        self.procs = {}          # job_id -> {popen, node}
        os.makedirs(PDIR, exist_ok=True)
        try:
            # a stale cached job script from an earlier hub version must
            # never outlive an agent restart (cost a shakedown, 2026-08-30)
            os.remove(JOB_SCRIPT)
        except Exception:
            pass
        self._fetch_script()

    def _fetch_script(self):
        if os.path.exists(JOB_SCRIPT):
            return
        try:
            req = urllib.request.Request(HUB + "/jobscript",
                                         headers={"User-Agent": "cei-node-agent/2.0"})
            with urllib.request.urlopen(req, timeout=5) as r:
                data = r.read()
            if data.startswith(b"#!/usr/bin/env python"):
                with open(JOB_SCRIPT, "wb") as f:
                    f.write(data)
        except Exception:
            pass                                    # retried next reconcile

    degraded_now = set()

    def _apply_degrade(self, nid, on):
        """Exp #6: a REAL straggler — lock the GPU's core clock low. The
        jobs there keep running, just slowly; only measured progress rates
        can reveal it. Reverted with -rgc."""
        if "-g" not in nid or MOCK_GPUS:
            return
        idx = nid.rsplit("g", 1)[-1]
        cmd = (["nvidia-smi", "-i", idx, "-lgc", "210,420"] if on
               else ["nvidia-smi", "-i", idx, "-rgc"])
        try:
            subprocess.run(cmd, capture_output=True, timeout=10)
            print(f"DEGRADE {'on' if on else 'off'} {nid}", flush=True)
        except Exception as e:
            print(f"degrade failed {nid}: {e}", flush=True)

    def reconcile(self, directives):
        want = {}                        # job_id -> (node, seconds, resume_s)
        for nid, d in directives.items():
            wants_degrade = bool(d.get("degraded"))
            if wants_degrade != (nid in self.degraded_now):
                self._apply_degrade(nid, wants_degrade)
                (self.degraded_now.add(nid) if wants_degrade
                 else self.degraded_now.discard(nid))
            if d.get("failed"):
                for jid, rec in list(self.procs.items()):
                    if rec["node"] == nid:
                        print(f"DEBUG kill-failed-node {jid} on {nid}", flush=True)
                        try:
                            rec["popen"].send_signal(signal.SIGKILL)
                        except Exception:
                            pass
                        # keep entry until reported dead once
                continue
            for j in d.get("jobs", []):
                want[j["id"]] = (nid, j["seconds"], j.get("resume_s", 0),
                                 j.get("ckpt_every_s"))
        self._fetch_script()
        # Two-way reconcile with a GRACE PERIOD: a live process is only a
        # stray if the hub has not wanted it for 3 consecutive cycles (kills
        # trial-reset ghosts within ~3 s while being immune to any one-cycle
        # want/have race, which caused repeated spurious kills when this was
        # immediate).
        for jid, rec in list(self.procs.items()):
            if jid in want or rec["popen"].poll() is not None:
                rec["unwanted"] = 0
                continue
            rec["unwanted"] = rec.get("unwanted", 0) + 1
            if rec["unwanted"] >= 3:
                try:
                    rec["popen"].send_signal(signal.SIGKILL)
                except Exception:
                    pass
                self.procs.pop(jid, None)
                try:
                    os.remove(os.path.join(PDIR, jid + ".json"))
                except Exception:
                    pass
        for jid, (nid, seconds, resume_s, ckpt_every) in want.items():
            if jid in self.procs and self.procs[jid]["popen"].poll() is None:
                continue
            if jid in self.procs:
                continue                            # finished; awaiting report
            dev = nid.rsplit("g", 1)[-1] if "-g" in nid else "cpu"
            env = dict(os.environ,
                       JOB_ID=jid, SECONDS=str(seconds),
                       RESUME_S=str(resume_s),
                       DEVICE=("cpu" if (ALLOW_CPU and MOCK_GPUS) else dev),
                       PROGRESS_DIR=PDIR)
            if ckpt_every:
                env["CKPT_EVERY_S"] = str(ckpt_every)
            try:
                # stale heartbeat from a previous trial with a reused job id
                # must never be read as this process's status
                os.remove(os.path.join(PDIR, jid + ".json"))
            except Exception:
                pass
            try:
                self.procs[jid] = {"node": nid, "popen": subprocess.Popen(
                    [sys.executable, JOB_SCRIPT], env=env,
                    stdout=subprocess.DEVNULL,
                    stderr=open(os.path.join(PDIR, jid + ".err"), "wb"))}
            except Exception as e:
                print("job spawn failed:", jid, e)

    def statuses(self):
        out = []
        for jid, rec in list(self.procs.items()):
            elapsed, done, hb = 0.0, False, {}
            try:
                with open(os.path.join(PDIR, jid + ".json")) as f:
                    hb = json.load(f)
                elapsed, done = hb.get("elapsed", 0.0), hb.get("done", False)
                ckpt = hb.get("ckpt", 0.0)
            except Exception:
                ckpt = 0.0
            rc = rec["popen"].poll()
            alive = rc is None
            if not alive and rc != 0:
                print(f"DEBUG dead {jid} rc={rc}", flush=True)
            out.append({"id": jid, "elapsed": elapsed, "ckpt": ckpt,
                        "ckpt_writes": hb.get("ckpt_writes", 0),
                        "ckpt_cost_s": hb.get("ckpt_cost_s", 0.0),
                        "done": done or (rc == 0), "alive": alive})
            if not alive:
                del self.procs[jid]                 # reported once, forget
                try:
                    os.remove(os.path.join(PDIR, jid + ".json"))
                except Exception:
                    pass
        return out

    def per_node_count(self):
        per = {}
        for rec in self.procs.values():
            if rec["popen"].poll() is None:
                per[rec["node"]] = per.get(rec["node"], 0) + 1
        return per


def post(nodes, job_status):
    req = urllib.request.Request(
        HUB + "/telemetry/node",
        data=json.dumps({"host": HOST, "nodes": nodes,
                         "job_status": job_status}).encode(),
        headers={"Content-Type": "application/json",
                 "User-Agent": "cei-node-agent/2.0"})
    with urllib.request.urlopen(req, timeout=3) as r:
        return json.loads(r.read())


def main():
    source = detect()
    runner = JobRunner()
    mock = MockBank(MOCK_GPUS, runner) if source == "mock" else None
    print(f"node_agent v2: host={HOST} source={source} hub={HUB}")
    misses = 0
    while True:
        t0 = time.time()
        try:
            nodes = mock.read() if mock else \
                (read_nvidia() if source == "nvidia" else read_pi())
            reply = post(nodes, runner.statuses())
            runner.reconcile(reply.get("directives", {}))
            misses = 0
        except Exception as e:
            misses += 1
            if misses in (1, 10, 60):
                print(f"node_agent: hub unreachable ({e}); retrying")
        time.sleep(max(0.0, 1.0 - (time.time() - t0)))


if __name__ == "__main__":
    main()
