#!/usr/bin/env python3
"""A REAL workload process — the unit of job accounting in the v2 demo.

Spawned by the node agent, one OS process per job:
    JOB_ID=j17 SECONDS=45 DEVICE=0 PROGRESS_DIR=/tmp/cei-jobs python3 gpu_job.py

Does real work (torch matmuls on the assigned GPU; CPU fallback for
GPU-less validation), writes a heartbeat JSON every second:
    {"job_id": ..., "elapsed": ..., "target": ..., "done": false}
and exits 0 on completion. If the node is killed (power loss / SIGKILL),
the heartbeat simply stops — the last written "elapsed" is the job's real
progress at death, which the hub counts as lost GPU-seconds.
"""
import json
import os
import time

JOB_ID = os.environ.get("JOB_ID", "j0")
SECONDS = float(os.environ.get("SECONDS", "30"))       # TOTAL work required
RESUME_S = float(os.environ.get("RESUME_S", "0"))      # checkpointed progress
CKPT_EVERY_S = float(os.environ.get("CKPT_EVERY_S", "10"))
DEVICE = os.environ.get("DEVICE", "cpu")          # "0","1",... or "cpu"
PDIR = os.environ.get("PROGRESS_DIR", "/tmp/cei-jobs")

os.makedirs(PDIR, exist_ok=True)
path = os.path.join(PDIR, JOB_ID + ".json")


def beat(elapsed, done):
    ckpt = (elapsed // CKPT_EVERY_S) * CKPT_EVERY_S     # durable every N s
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"job_id": JOB_ID, "elapsed": round(elapsed, 1),
                   "ckpt": round(ckpt, 1), "target": SECONDS, "done": done}, f)
    os.replace(tmp, path)


def main():
    use_torch = True
    try:
        import torch
        if DEVICE != "cpu" and torch.cuda.is_available():
            dev = torch.device(f"cuda:{DEVICE}")
            n = 2048
        else:
            dev = torch.device("cpu")
            n = 256
        a = torch.randn(n, n, device=dev)
        b = torch.randn(n, n, device=dev)
    except Exception:
        use_torch = False

    t0 = time.time()
    last_beat = 0.0
    beat(RESUME_S, False)
    while True:
        elapsed = RESUME_S + (time.time() - t0)   # total progress incl. resume
        if elapsed >= SECONDS:
            break
        if use_torch:
            for _ in range(8):
                a = a @ b
                a = a / a.norm()
            if DEVICE != "cpu":
                try:
                    import torch
                    torch.cuda.synchronize()
                except Exception:
                    pass
        else:
            x = 1.0001
            for _ in range(200000):
                x = x * 1.0000001
        if elapsed - last_beat >= 1.0:
            beat(elapsed, False)
            last_beat = elapsed
    beat(SECONDS, True)


if __name__ == "__main__":
    main()
