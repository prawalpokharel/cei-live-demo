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
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

from . import cei as cei_mod
from .controller import Controller, EPOCH_S
from .jobs import JobManager
from .registry import Registry
from .scheduler import Scheduler
from .telemetry import MOCK, LocalMockSource

SETPOINT_C = float(os.environ.get("SETPOINT_C", "45"))
BAND_C = float(os.environ.get("BAND_C", "40"))
# node ids that host a shared service other jobs depend on (exp #2),
# parsed from SVC hosts if the agents advertise them; empty by default.
DOMAIN_SERVICE_NODES = set(
    x for x in os.environ.get("DEP_SERVICE_NODES", "").split(",") if x)


def compute_cei():
    """Per-node live CEI scores from current measured state."""
    return cei_mod.node_scores(
        reg.snapshot(), jm.running_detail(),
        SETPOINT_C, BAND_C, DOMAIN_SERVICE_NODES)

HERE = os.path.dirname(__file__)
GHOST_PATH = os.path.join(HERE, "..", "ghost.json")

app = FastAPI(title="CEI live demo hub v2")
# Allow the product's /live page (and local dev) to read this hub's metrics.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://gpu.iversoncloud.com", "http://localhost:3100",
                   "http://localhost:3000"],
    allow_methods=["GET", "POST"], allow_headers=["*"])
reg = Registry()
ctl = Controller()
sched = Scheduler(reg)
jm = JobManager(reg, mock=MOCK)
mock = LocalMockSource(reg) if MOCK else None


# ---- telemetry poisoning (experiment #9) --------------------------------
# The controller's VIEW of temperature can be delayed, dropped, or offset;
# the raw ledger (energy, dashboards, job accounting) always stays real.
import collections
import random as _random

POISON = {"delay_s": 0.0, "drop_pct": 0.0}
_hist = collections.defaultdict(lambda: collections.deque(maxlen=90))
_poison_rng = _random.Random(4242)
_last_seen = {}

# Auto-evacuation state (exp #6) — MUST be defined before _loop, which the
# background thread starts running at import; referencing it from a later
# module position crashed the loop thread with NameError (no arrivals).
AUTO_EVAC = {"on": False, "threshold": 0.55, "consecutive": 4}
_gp_low = {}


def _controller_temps():
    now = time.time()
    temps = []
    for n in reg.healthy():
        _hist[n.id].append((now, n.temp))
        t = n.temp
        if POISON["delay_s"] > 0:
            cutoff = now - POISON["delay_s"]
            aged = [v for ts, v in _hist[n.id] if ts <= cutoff]
            t = aged[-1] if aged else _hist[n.id][0][1]
        if POISON["drop_pct"] > 0 and _poison_rng.random() * 100 < POISON["drop_pct"]:
            t = _last_seen.get(n.id, t)     # dropped report: view freezes
        _last_seen[n.id] = t
        temps.append(t)
    return temps


def _loop():
    last_epoch = 0.0
    while True:
        if mock:
            mock.tick()
        jm.tick(sched.running)
        jm.domain_ids = frozenset(reg.domain_ids())   # adaptive ckpt (#7)
        gp = jm.node_goodput()                        # measured rates (#6)
        if AUTO_EVAC["on"]:
            for nid, rate in gp.items():
                node = reg.nodes.get(nid)
                if node is None or node.cordoned or node.failed:
                    continue
                if rate < AUTO_EVAC["threshold"]:
                    _gp_low[nid] = _gp_low.get(nid, 0) + 1
                    if _gp_low[nid] >= AUTO_EVAC["consecutive"]:
                        print(f"AUTO-EVAC {nid}: goodput {rate:.2f} < "
                              f"{AUTO_EVAC['threshold']} for "
                              f"{_gp_low[nid]} ticks", flush=True)
                        reg.cordon_match(nid, True)
                        jm.evacuate_nodes({nid})
                        _gp_low.pop(nid, None)
                else:
                    _gp_low[nid] = 0
        now = time.time()
        if now - last_epoch >= EPOCH_S:
            last_epoch = now
            temps = _controller_temps()
            lam = ctl.step(max(temps) if temps else 0.0)
            if sched.running:
                active = ([n.id for n in reg.healthy()] if sched.random_place
                          else sched.active_set(lam))
                jm.assign(active, random_place=sched.random_place)
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


@app.get("/svcscript")
def svc_script():
    return FileResponse(os.path.join(HERE, "..", "jobs", "svc_writer.py"),
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
            "goodput": {k: round(v, 3) for k, v in jm._gp_ema.items()},
            "auto_evac": dict(AUTO_EVAC),
            "cei": compute_cei(),
            "modeled": jm.snapshot()}       # back-compat alias


@app.get("/whatif")
def whatif(node: str):
    """Counterfactual for one node: predicted blast radius if it fails now."""
    scores = compute_cei()
    return cei_mod.what_if(node, scores, jm.running_detail(),
                           jm.snapshot().get("avg_recovery_s"))


@app.get("/cei")
def cei_endpoint():
    """Ranked live CEI scores — the risk engine's current read of the fleet."""
    scores = compute_cei()
    ranked = sorted(scores.items(), key=lambda kv: kv[1]["cei"], reverse=True)
    return {"nodes": scores,
            "ranked": [{"node": k, **v} for k, v in ranked],
            "setpoint_c": SETPOINT_C}


# ---- controls ------------------------------------------------------------
@app.post("/control/start")
def start():
    sched.running = True
    return {"ok": True}


@app.post("/control/mode")
def mode(body: dict):
    m = body.get("mode", "auto")
    ctl.set_mode(m, body.get("lam"))
    # AUTO = CEI proper; FIXED = gamma=0 strawman. An explicit "centrality"
    # boolean overrides — the ablation arm is AUTO with centrality off.
    c = body.get("centrality")
    sched.centrality = (m == "auto") if c is None else bool(c)
    sched.random_place = bool(body.get("placement") == "random")
    return ctl.snapshot()


@app.post("/control/kill_domain")
def kill_domain(body: dict = None):
    match = (body or {}).get("match")
    ids = set(reg.fail_match(match) if match else reg.fail_domain())
    hit, running = jm.fail_nodes(ids)
    return {"ok": True, "jobs_interrupted_now": hit,
            "jobs_running_now": running, "failed_nodes": sorted(ids)}


@app.post("/control/reset")
def reset(body: dict = None):
    reg.reset()
    jm.reset(seed=(body or {}).get("seed"))
    sched.running = False
    return {"ok": True}


@app.post("/control/probe")
def probe(body: dict = None):
    """Exp #2: suspend everything on matching nodes so the DEPENDENCY
    response of the rest of the cluster can be measured.
    {"match": "-g2", "on": true|false}"""
    b = body or {}
    ids = reg.probe_match(b.get("match", ""), b.get("on", True))
    return {"ok": True, "probed": ids, "on": b.get("on", True)}


@app.post("/control/degrade")
def degrade(body: dict = None):
    """Exp #6: mark nodes degraded — agents apply a REAL clock-lock there.
    {"match": "-g4", "on": true|false}"""
    b = body or {}
    ids = reg.degrade_match(b.get("match", ""), b.get("on", True))
    return {"ok": True, "degraded": ids, "on": b.get("on", True)}


@app.post("/control/evacuate")
def evacuate(body: dict = None):
    """Exp #6: cordon nodes and gracefully requeue their jobs (loss bounded
    by checkpoint age). {"match": "-g4", "on": true|false}"""
    b = body or {}
    ids = reg.cordon_match(b.get("match", ""), b.get("on", True))
    moved = jm.evacuate_nodes(set(ids)) if b.get("on", True) else 0
    return {"ok": True, "cordoned": ids, "jobs_evacuated": moved}


@app.post("/control/auto_evacuate")
def auto_evacuate(body: dict = None):
    """Exp #6: let the hub itself detect slow-not-dead nodes from measured
    job-progress rates and evacuate them. {"on": true, "threshold": 0.55}"""
    b = body or {}
    AUTO_EVAC["on"] = bool(b.get("on", True))
    AUTO_EVAC["threshold"] = float(b.get("threshold", 0.55))
    _gp_low.clear()
    return {"ok": True, **AUTO_EVAC}


@app.post("/control/ckpt")
def ckpt_policy(body: dict = None):
    """Experiment #7: checkpoint policy = default | fixed:<sec> | adaptive."""
    jm.ckpt_policy = (body or {}).get("policy", "default")
    return {"ok": True, "ckpt_policy": jm.ckpt_policy}


@app.post("/control/telemetry")
def telemetry_poison(body: dict = None):
    """Experiment #9: poison the CONTROLLER's temperature view only."""
    b = body or {}
    POISON["delay_s"] = float(b.get("delay_s", 0.0))
    POISON["drop_pct"] = float(b.get("drop_pct", 0.0))
    return {"ok": True, **POISON}


@app.post("/control/revive")
def revive():
    """Clear node failed flags WITHOUT touching jobs — models a transient
    failure (node reboots, rejoins the pool). Lets the failure-rate
    frontier study inject several failures per trial against the same
    domain instead of exhausting distinct targets."""
    reg.reset()
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
