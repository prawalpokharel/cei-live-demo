"""FastAPI hub v2: node registry + REAL job accounting + controller loop.

Run the hub:
    MOCK=1 uvicorn server.app:app --host 0.0.0.0 --port 8000   # $0 rehearsal
    uvicorn server.app:app --host 0.0.0.0 --port 8000          # real nodes

Real nodes run agents/node_agent.py; jobs are real OS processes
(jobs/gpu_job.py) spawned by agents per the hub's placement. The job
ARRIVAL schedule is synthetic (scripted Poisson) — execution, interrupts,
lost GPU-seconds and recovery times are measured from real processes.

Domain: DOMAIN_MATCH=<substring> (e.g. b-). Dashboard at :8000.
"""
import json
import os
import threading
import time

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse

from .controller import Controller, EPOCH_S
from .jobs import JobManager
from .registry import Registry
from .scheduler import Scheduler
from .telemetry import MOCK, LocalMockSource

HERE = os.path.dirname(__file__)
GHOST_PATH = os.path.join(HERE, "..", "ghost.json")

app = FastAPI(title="CEI live demo hub v2")
reg = Registry()
ctl = Controller()
sched = Scheduler(reg)
jm = JobManager(reg, mock=MOCK)
mock = LocalMockSource(reg) if MOCK else None


def _loop():
    last_epoch = 0.0
    while True:
        if mock:
            mock.tick()
        jm.tick(sched.running)
        now = time.time()
        if now - last_epoch >= EPOCH_S:
            last_epoch = now
            temps = [n.temp for n in reg.healthy()]
            lam = ctl.step(max(temps) if temps else 0.0)
            if sched.running:
                jm.assign(sched.active_set(lam))
        sched.apply_thermal(jm.snapshot()["jobs_per_node"])
        time.sleep(1.0)


threading.Thread(target=_loop, daemon=True).start()


@app.get("/")
def index():
    return FileResponse(os.path.join(HERE, "static", "index.html"))


@app.get("/agent")
def agent_file():
    return FileResponse(os.path.join(HERE, "..", "agents", "node_agent.py"),
                        media_type="text/x-python")


@app.get("/jobscript")
def job_script():
    return FileResponse(os.path.join(HERE, "..", "jobs", "gpu_job.py"),
                        media_type="text/x-python")


# ---- agent report + pull-based actuation --------------------------------
@app.post("/telemetry/node")
def node_report(body: dict):
    ids = []
    for n in body.get("nodes", []):
        reg.upsert(n["id"], n.get("kind", "gpu"), n.get("temp", 0),
                   n.get("watts", 0), n.get("util", 0))
        ids.append(n["id"])
    if body.get("job_status"):
        jm.agent_status(body.get("host", "?"), body["job_status"])
    d = reg.directives_for(ids)
    jobs = jm.jobs_for(ids)
    for nid in d:
        d[nid]["jobs"] = jobs.get(nid, [])
    return {"directives": d, "running": sched.running}


@app.get("/metrics")
def metrics():
    m = reg.snapshot()
    m["energy_src"] = "integrated from reported watts @1Hz"
    m["mock"] = MOCK
    return {"measured": m, "controller": ctl.snapshot(),
            "jobs": jm.snapshot(),
            "modeled": jm.snapshot()}       # back-compat alias


# ---- controls ------------------------------------------------------------
@app.post("/control/start")
def start():
    sched.running = True
    return {"ok": True}


@app.post("/control/mode")
def mode(body: dict):
    m = body.get("mode", "auto")
    ctl.set_mode(m, body.get("lam"))
    sched.centrality = (m == "auto")   # AUTO = CEI proper; FIXED = gamma=0
    return ctl.snapshot()


@app.post("/control/kill_domain")
def kill_domain():
    ids = set(reg.fail_domain())
    hit = jm.fail_nodes(ids)
    return {"ok": True, "jobs_interrupted_now": hit}


@app.post("/control/reset")
def reset():
    reg.reset()
    jm.reset()
    return {"ok": True}


@app.post("/control/ghost/save")
def ghost_save():
    c = ctl.snapshot()
    s = jm.snapshot()
    snap = {"interrupted": s["interrupted_events"],
            "lost_gpu_seconds": s["lost_gpu_seconds"],
            "completed": s["completed"],
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
    return JSONResponse({"interrupted": None}, status_code=200)
