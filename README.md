# cei-live-demo — reproduce the IEMCON 2026 live demo for about $1

The live segment of *"Self-Tuning Governance of the Energy–Resilience Tradeoff
in GPU Cluster Scheduling"* (IEEE IEMCON 2026): the paper's hysteresis-band /
AIMD governance controller closing its loop on **real GPU watts and
temperatures**, on a rented node, with a correlated failure of a shared-chassis
"cooling domain" triggered live.

The paper's stated future work is *measured-power deployment on physical
nodes*. This repo is that step, small enough to run on stage — or by you,
tonight, for about a dollar of GPU time.

```bash
git clone https://github.com/prawalpokharel/cei-live-demo && cd cei-live-demo
./demo.sh          # real GPUs if present; mock thermal model otherwise
# open http://localhost:8000
```

## MEASURED vs MODELED (read this before quoting anything)

| MEASURED (live, real) | MODELED (computed, labeled) |
|---|---|
| Per-GPU watts (`power.draw`) | The synthetic job stream (arrivals, durations) |
| Per-GPU temperature (°C) | The jobs-lost tally |
| Utilization | The paper's fleet-scale numbers (844 Wh/job, 8%, 5.5×) — those come from the 320-GPU trace-driven simulation, in the [paper repo](https://github.com/prawalpokharel/self-tuning-cei-scheduler) |
| Integrated energy (Wh; DCGM counter when available) | |

The dashboard draws this line in its own layout. The red line on the chart is
a **software governance setpoint** (default 73 °C) — the operator's thermal
budget the controller is told to hold, *not* the silicon's throttle limit.
The controller is the paper's control law, **re-parameterized** for a rented
4090's much faster thermal loop (shorter epochs; same shape: `+η` cool,
hold in the dead band, `−6η` on violation, clip `[0.15, 0.90]`).

## Run it on a rented node (the real thing)

1. Rent a multi-GPU pod (e.g. RunPod Secure Cloud, 6× RTX 4090, ~$4.4/hr —
   or a single 4090 at ~$0.35/hr, which is the "about $1" version). Whole
   cards only: if `nvidia-smi` reports `power.draw` as `N/A` you got a
   MIG/vGPU slice; `demo.sh` checks and refuses.
2. `./demo.sh` — builds [gpu-burn](https://github.com/wilicc/gpu-burn),
   starts everything in a `tmux` session (survives SSH drops).
3. Open `https://<POD_ID>-8000.proxy.runpod.net` (printed by the script).
4. Rehearsal knob: with continuous burns, per-GPU temperature under "spread"
   falls less than under duty-cycled load — see the note in
   `server/scheduler.py` and tune the setpoint from the measured plateau.

## The stage flow

1. **Start job stream** · mode starts at **FIXED λ=0.85** (the "tuned
   elsewhere" weight). Load packs onto the domain tier (GPUs 0–1); watch them
   heat past the setpoint.
2. Trigger the domain kill once, **Save as ghost** — that captured fixed-λ
   loss count becomes the labeled "earlier run" overlay. **Reset.**
3. **AUTO** — the controller backs λ off as measured temperature nears the
   setpoint; load spreads (centrality-aware: the domain tier is used *last*
   when spreading); the line settles at the edge.
4. Press **A** to ARM, hand the red button (any USB HID button that types
   Enter/Space) to a volunteer. Their press kills the workload on the
   shared-chassis domain. Compare the live loss to the ghost.

## What's in here

```
server/         FastAPI app: 1 Hz telemetry, controller epoch loop, dashboard
  telemetry.py  nvidia-smi @1 Hz (or mock first-order thermal model)
  controller.py the AIMD/hysteresis law, re-parameterized (constants in env)
  scheduler.py  λ → active-GPU set (domain-first when packing, domain-LAST
                when spreading); gpu-burn reconciliation; synthetic jobs
  static/       the dashboard (MEASURED / MODELED zones)
dial/           the physical λ-knob: ESP32 firmware + build guide (~$30)
demo.sh         one command, real or mock
STAGE-RUNBOOK.md  demo-day pre-flight checklist
```

Paper + full reproducibility (simulator, Philly trace adapter, ERCOT data):
**github.com/prawalpokharel/self-tuning-cei-scheduler** · Product:
**gpu.iversoncloud.com** · Patent pending, U.S. App. No. 64/138,779.
