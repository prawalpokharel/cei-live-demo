#!/usr/bin/env python3
"""Universal node agent — runs on anything, reports to the hub at 1 Hz.

    HUB=https://<podid>-8000.proxy.runpod.net HOST=a python3 node_agent.py
    HUB=http://192.168.8.10:8000 HOST=pi1 python3 node_agent.py      # Pi
    HUB=http://localhost:8123 HOST=b MOCK_GPUS=2 python3 node_agent.py

Stdlib only — no pip installs on a fresh pod or Pi. Each 1 Hz report's
REPLY carries the hub's desired intensity per node (pull-based actuation:
nodes never need inbound network access), and the agent reconciles local
load to match:
  NVIDIA GPUs  -> one gpu-burn process per active GPU  (GPU_BURN=path)
  Raspberry Pi -> stress-ng --cpu N while active       (auto-detected)
  MOCK_GPUS=k  -> k simulated GPUs (thermal model), for $0 rehearsals

Telemetry sources (auto-detected, in order): nvidia-smi (GPU temp/power/
util), vcgencmd (Pi SoC temp; watts unavailable -> reported 0 and the hub
labels wall-watts as the Pi's meter source), mock.

MEASURED discipline: this agent only ever reports what a sensor said.
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
GPU_BURN = os.environ.get("GPU_BURN", "./gpu-burn/gpu_burn")
STRESS_CPUS = os.environ.get("STRESS_CPUS", "4")

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
    print("!! no nvidia-smi or vcgencmd; set MOCK_GPUS=n to simulate")
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
             "watts": 0.0, "util": 0.0}]   # Pi watts come from its wall meter


class MockBank:
    def __init__(self, k):
        self.temps = {f"{HOST}-g{i}": _T_IDLE + random.uniform(-1, 1)
                      for i in range(k)}
        self.intensity = {nid: 0.0 for nid in self.temps}

    def read(self):
        nodes = []
        for nid, t in self.temps.items():
            x = self.intensity[nid]
            target = _T_IDLE + (_T_BURN - _T_IDLE) * (x ** 0.7)
            t += (target - t) * _ALPHA + random.uniform(-0.3, 0.3)
            self.temps[nid] = t
            w = (_W_IDLE + (_W_BURN - _W_IDLE) * x) * random.uniform(0.96, 1.03)
            nodes.append({"id": nid, "kind": "mock-gpu", "temp": round(t, 1),
                          "watts": round(w, 1), "util": round(99.0 * x, 1)})
        return nodes


class Burns:
    """Reconcile real load processes to the hub's directives."""
    def __init__(self, source):
        self.source = source
        self.procs = {}

    def reconcile(self, directives):
        for nid, d in directives.items():
            want = d["intensity"] > 0 and not d["failed"]
            have = nid in self.procs and self.procs[nid].poll() is None
            if want and not have:
                self.procs[nid] = self._start(nid)
            elif not want and have:
                self.procs[nid].send_signal(signal.SIGKILL)
                self.procs.pop(nid, None)

    def _start(self, nid):
        if self.source == "nvidia":
            idx = nid.rsplit("g", 1)[1]
            return subprocess.Popen([GPU_BURN, "-i", idx, "36000"],
                                    stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL)
        return subprocess.Popen(["stress-ng", "--cpu", STRESS_CPUS,
                                 "--timeout", "36000"],
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)


def post(nodes):
    req = urllib.request.Request(
        HUB + "/telemetry/node",
        data=json.dumps({"host": HOST, "nodes": nodes}).encode(),
        headers={"Content-Type": "application/json",
                 "User-Agent": "cei-node-agent/1.0"})
    with urllib.request.urlopen(req, timeout=3) as r:
        return json.loads(r.read())


def main():
    source = detect()
    mock = MockBank(MOCK_GPUS) if source == "mock" else None
    burns = None if source == "mock" else Burns(source)
    print(f"node_agent: host={HOST} source={source} hub={HUB}")
    misses = 0
    while True:
        t0 = time.time()
        try:
            nodes = mock.read() if mock else \
                (read_nvidia() if source == "nvidia" else read_pi())
            reply = post(nodes)
            d = reply.get("directives", {})
            if mock:
                for nid, dd in d.items():
                    mock.intensity[nid] = 0.0 if dd["failed"] else dd["intensity"]
            elif burns:
                burns.reconcile(d)
            misses = 0
        except Exception as e:
            misses += 1
            if misses in (1, 10, 60):
                print(f"node_agent: hub unreachable ({e}); retrying")
        time.sleep(max(0.0, 1.0 - (time.time() - t0)))


if __name__ == "__main__":
    main()
