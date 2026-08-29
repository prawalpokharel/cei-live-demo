"""FastAPI hub: node registry, epoch controller loop, dashboard, controls.

Run the hub:
    MOCK=1 uvicorn server.app:app --host 0.0.0.0 --port 8000   # $0 rehearsal
    uvicorn server.app:app --host 0.0.0.0 --port 8000          # real nodes

Real nodes (rented pods, the NVIDIA laptop, Raspberry Pis) each run
    HUB=<hub url> HOST=<name> python3 agents/node_agent.py
and appear in the registry automatically. On a RunPod pod the dashboard is
    https://[POD_ID]-8000.proxy.runpod.net
(short-polled every 1 s by the page — no long-lived streams; the RunPod
proxy caps connections at 100 s).

Domain selection: DOMAIN_MATCH=<substring> (e.g. DOMAIN_MATCH=b- makes every
node on host "b" the shared high-centrality domain). Unset: first two nodes.
"""
import json
import os
import threading
import time

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse

from .controller import Controller, EPOCH_S
from .registry import Registry
from .scheduler import Scheduler
from .telemetry import MOCK, LocalMockSource

HERE = os.path.dirname(__file__)
GHOST_PATH = os.path.join(HERE, "..", "ghost.json")

app = FastAPI(title="CEI live demo hub")
reg = Registry()
ctl = Controller()
sched = Scheduler(reg)
mock = LocalMockSource(reg) if MOCK else None


def _loop():
    last_epoch = 0.0
    while True:
        if mock:
            mock.tick()
        lam = ctl.snapshot()["lam"]
        sched.step_jobs(lam, 1.0)
        now = time.time()
        if now - last_epoch >= EPOCH_S:
            last_epoch = now
            temps = [n.temp for n in reg.healthy()]
            lam = ctl.step(max(temps) if temps else 0.0)
            sched.apply(lam)
        time.sleep(1.0)


threading.Thread(target=_loop, daemon=True).start()


@app.get("/")
def index():
    return FileResponse(os.path.join(HERE, "static", "index.html"))


@app.get("/agent")
def agent_file():
    """Serve the node agent so a new node needs only:
    curl -s <hub>/agent -o node_agent.py"""
    return FileResponse(os.path.join(HERE, "..", "agents", "node_agent.py"),
                        media_type="text/x-python")


# ---- agent report + pull-based actuation --------------------------------
@app.post("/telemetry/node")
def node_report(body: dict):
    ids = []
    for n in body.get("nodes", []):
        nid = n["id"]
        reg.upsert(nid, n.get("kind", "gpu"), n.get("temp", 0),
                   n.get("watts", 0), n.get("util", 0))
        ids.append(nid)
    return {"directives": reg.directives_for(ids),
            "running": sched.running}


@app.get("/metrics")
def metrics():
    m = reg.snapshot()
    m["energy_src"] = "integrated from reported watts @1Hz"
    m["mock"] = MOCK
    return {"measured": m, "controller": ctl.snapshot(),
            "modeled": sched.snapshot()}


# ---- controls ------------------------------------------------------------
@app.post("/control/start")
def start():
    sched.running = True
    sched.apply(ctl.snapshot()["lam"])
    return {"ok": True}


@app.post("/control/mode")
def mode(body: dict):
    m = body.get("mode", "auto")
    ctl.set_mode(m, body.get("lam"))
    sched.centrality = (m == "auto")   # AUTO = CEI proper; FIXED = gamma=0
    return ctl.snapshot()


@app.post("/control/kill_domain")
def kill_domain():
    lost = sched.kill_domain()
    return {"ok": True, "jobs_lost_now": lost}


@app.post("/control/reset")
def reset():
    sched.reset()
    return {"ok": True}


@app.post("/control/ghost/save")
def ghost_save():
    c = ctl.snapshot()
    snap = {"jobs_lost": sched.snapshot()["jobs_lost"],
            "label": f"earlier run ({c['mode']} λ={c['lam']})",
            "saved_at": time.strftime("%H:%M")}
    with open(GHOST_PATH, "w") as f:
        json.dump(snap, f)
    return snap


@app.get("/ghost")
def ghost():
    if os.path.exists(GHOST_PATH):
        with open(GHOST_PATH) as f:
            return json.load(f)
    return JSONResponse({"jobs_lost": None}, status_code=200)
