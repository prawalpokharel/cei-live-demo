#!/usr/bin/env python3
"""A REAL workload process — the unit of job accounting in the v2+ demo.

Spawned by the node agent, one OS process per job:
    JOB_ID=j17 SECONDS=45 DEVICE=0 PROGRESS_DIR=/tmp/cei-jobs python3 gpu_job.py

Does real work (torch matmuls on the assigned GPU; CPU fallback for
GPU-less validation) and writes a heartbeat JSON every second:
    {"job_id", "elapsed", "ckpt", "ckpt_writes", "ckpt_cost_s", "target", "done"}

CHECKPOINTS ARE REAL (v3.1, experiment #7): crossing a CKPT_EVERY_S
boundary serializes the working tensor to disk (torch.save, ~16 MB at
n=2048) and the measured wall cost of every save is accumulated in
ckpt_cost_s — so the lost-work vs checkpoint-overhead tradeoff is
measured, not assumed. If the node is killed, the heartbeat simply
stops — the last written "elapsed" is the job's real progress at death,
and "ckpt" is the durable resume point.
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
ckpt_path = os.path.join(PDIR, JOB_ID + ".ckpt")

state = {"writes": 0, "cost_s": 0.0}


def beat(elapsed, ckpt, done):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"job_id": JOB_ID, "elapsed": round(elapsed, 1),
                   "ckpt": round(ckpt, 1), "target": SECONDS, "done": done,
                   "ckpt_writes": state["writes"],
                   "ckpt_cost_s": round(state["cost_s"], 3)}, f)
    os.replace(tmp, path)


def save_ckpt(tensor):
    """Real serialization to disk; measured cost accumulates."""
    t0 = time.time()
    try:
        if tensor is not None:
            import torch
            torch.save(tensor.cpu(), ckpt_path + ".tmp")
        else:
            with open(ckpt_path + ".tmp", "w") as f:
                json.dump({"job_id": JOB_ID, "ts": time.time()}, f)
        os.replace(ckpt_path + ".tmp", ckpt_path)
    except Exception:
        pass
    state["writes"] += 1
    state["cost_s"] += time.time() - t0


def main():
    use_torch = True
    a = b = None
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
    ckpt = (RESUME_S // CKPT_EVERY_S) * CKPT_EVERY_S
    beat(RESUME_S, ckpt, False)
    while True:
        # elapsed excludes time spent inside checkpoint saves: checkpoint
        # cost is real overhead, not job progress
        elapsed = RESUME_S + (time.time() - t0) - state["cost_s"]
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
        boundary = (elapsed // CKPT_EVERY_S) * CKPT_EVERY_S
        if boundary > ckpt:
            save_ckpt(a if use_torch else None)
            ckpt = boundary
        if elapsed - last_beat >= 1.0:
            beat(elapsed, ckpt, False)
            last_beat = elapsed
    beat(SECONDS, SECONDS, True)


if __name__ == "__main__":
    main()
