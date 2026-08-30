#!/usr/bin/env python3
"""Experiment #5: communication locality vs resilience — measured.

Runs a synchronous all_reduce ring over N GPUs (torch.distributed, NCCL;
gloo/CPU fallback for $0 validation) and measures collective goodput in
iterations/sec across three phases:

  A. all N GPUs healthy            -> baseline collective throughput
  B. one GPU clock-locked (done   -> the whole synchronous ring drags to
     externally via nvidia-smi)      the straggler's pace ("slow is worse
                                     than dead")
  C. ring rebuilt WITHOUT the      -> N-1 fast GPUs beat N crippled ones:
     straggler                       the measured case for isolation

    python3 scripts/nccl_bench.py --world 8 --seconds 45 --out nccl.json
    (phase B: from another shell, nvidia-smi -i 3 -lgc 210,420)

Prints per-phase iterations/sec; every number is measured wall-clock over
real collectives on physical GPUs.
"""
import argparse
import json
import os
import time

import torch
import torch.distributed as dist
import torch.multiprocessing as mp


def worker(rank, world, seconds, backend, size_mb, q, exclude):
    if rank in exclude:
        return
    ranks = [r for r in range(world) if r not in exclude]
    os.environ.setdefault("MASTER_ADDR", "127.0.0.1")
    os.environ.setdefault("MASTER_PORT", "29517")
    dist.init_process_group(backend, rank=ranks.index(rank),
                            world_size=len(ranks))
    dev = torch.device(f"cuda:{rank}") if backend == "nccl" else torch.device("cpu")
    if backend == "nccl":
        torch.cuda.set_device(dev)
    n = int(size_mb * 1024 * 1024 / 4)
    t = torch.randn(n, device=dev)
    # warmup
    for _ in range(3):
        dist.all_reduce(t)
    if backend == "nccl":
        torch.cuda.synchronize()
    t0 = time.time()
    iters = 0
    while time.time() - t0 < seconds:
        dist.all_reduce(t)
        t = t / t.norm()
        if backend == "nccl":
            torch.cuda.synchronize()
        iters += 1
    wall = time.time() - t0
    if ranks.index(rank) == 0:
        q.put({"iters": iters, "wall": round(wall, 2),
               "iters_per_s": round(iters / wall, 3),
               "world": len(ranks), "backend": backend,
               "tensor_mb": size_mb})
    dist.destroy_process_group()


def run_phase(world, seconds, backend, size_mb, exclude=()):
    q = mp.get_context("spawn").Queue()
    ctx = mp.get_context("spawn")
    procs = [ctx.Process(target=worker,
                         args=(r, world, seconds, backend, size_mb, q,
                               tuple(exclude)))
             for r in range(world)]
    for p in procs:
        p.start()
    res = q.get(timeout=seconds + 120)
    for p in procs:
        p.join(timeout=60)
        if p.is_alive():
            p.terminate()
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--world", type=int, default=8)
    ap.add_argument("--seconds", type=int, default=45)
    ap.add_argument("--size-mb", type=float, default=64)
    ap.add_argument("--straggler", type=int, default=3,
                    help="rank to clock-lock in phase B / exclude in C")
    ap.add_argument("--backend", default=None,
                    help="nccl|gloo (default: nccl if cuda else gloo)")
    ap.add_argument("--phase", default="A",
                    help="A=baseline, B=with straggler (lock it first), "
                         "C=straggler excluded")
    ap.add_argument("--out", default="nccl_phase.json")
    args = ap.parse_args()
    backend = args.backend or ("nccl" if torch.cuda.is_available() else "gloo")
    exclude = (args.straggler,) if args.phase == "C" else ()
    res = run_phase(args.world, args.seconds, backend, args.size_mb, exclude)
    res["phase"] = args.phase
    res["straggler_rank"] = args.straggler if args.phase != "A" else None
    print(json.dumps(res))
    with open(args.out, "w") as f:
        json.dump(res, f, indent=1)


if __name__ == "__main__":
    main()
