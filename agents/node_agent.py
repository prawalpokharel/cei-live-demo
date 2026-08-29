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

    def reconcile(self, directives):
        want = {}                                   # job_id -> (node, seconds)
        for nid, d in directives.items():
            if d.get("failed"):
                for jid, rec in list(self.procs.items()):
                    if rec["node"] == nid:
                        try:
                            rec["popen"].send_signal(signal.SIGKILL)
                        except Exception:
                            pass
                        # keep entry until reported dead once
                continue
            for j in d.get("jobs", []):
                want[j["id"]] = (nid, j["seconds"])
        self._fetch_script()
        for jid, (nid, seconds) in want.items():
            if jid in self.procs and self.procs[jid]["popen"].poll() is None:
                continue
            if jid in self.procs:
                continue                            # finished; awaiting report
            dev = nid.rsplit("g", 1)[-1] if "-g" in nid else "cpu"
            env = dict(os.environ,
                       JOB_ID=jid, SECONDS=str(seconds),
                       DEVICE=("cpu" if (ALLOW_CPU and MOCK_GPUS) else dev),
                       PROGRESS_DIR=PDIR)
            try:
                self.procs[jid] = {"node": nid, "popen": subprocess.Popen(
                    [sys.executable, JOB_SCRIPT], env=env,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)}
            except Exception as e:
                print("job spawn failed:", jid, e)

    def statuses(self):
        out = []
        for jid, rec in list(self.procs.items()):
            elapsed, done = 0.0, False
            try:
                with open(os.path.join(PDIR, jid + ".json")) as f:
                    hb = json.load(f)
                elapsed, done = hb.get("elapsed", 0.0), hb.get("done", False)
            except Exception:
                pass
            rc = rec["popen"].poll()
            alive = rc is None
            out.append({"id": jid, "elapsed": elapsed,
                        "done": done or (rc == 0), "alive": alive})
            if not alive:
                del self.procs[jid]                 # reported once, forget
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
