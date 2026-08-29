# Test day — rent ~10 real GPUs for a couple of hours

Goal: prove the whole pipeline on real rented silicon — agents register, real
watts/temps flow, the controller governs, the domain dies on the button —
so the only thing left before the conference is rehearsal polish.

**Cost: ~$15–25 for two hours.** Per-second billing; stop the pods when done.

## What to rent (RunPod → Secure Cloud)

Two pods, so the domain is a REAL separate host:

| Pod | Config | Role | ~$/hr |
|---|---|---|---|
| **A** (the hub) | 8× RTX 4090, Secure Cloud, whole cards | 8 compute nodes + runs the hub | ~$5.9 |
| **B** (the domain) | 2× RTX 4090, Secure Cloud | the shared high-centrality domain | ~$1.5 |

On pod A, expose **port 8000 (HTTP)** in the pod config. Cheap variant for a
first plumbing test: two 2×4090 pods (~$3/hr total) — same steps, 4 nodes.

## Bring-up (paste per pod)

**Pod A — hub + its 8 local nodes:**
```bash
git clone https://github.com/prawalpokharel/cei-live-demo && cd cei-live-demo
DOMAIN_MATCH=b- ./demo.sh          # builds gpu-burn, starts hub + local agent (HOST=a) in tmux
```
Note the printed public URL: `https://<PODA_ID>-8000.proxy.runpod.net`

**Pod B — agent only:**
```bash
git clone https://github.com/prawalpokharel/cei-live-demo && cd cei-live-demo
git clone --depth 1 https://github.com/wilicc/gpu-burn && (cd gpu-burn && make)
tmux new -d -s agent "HUB=https://<PODA_ID>-8000.proxy.runpod.net HOST=b python3 agents/node_agent.py"
```

Open the pod-A URL in a browser: **10 tiles**, `b-g0`/`b-g1` tagged DOMAIN.

## The verification arc (~20 min)

1. **Registration:** all 10 nodes present, none stale; watts are real numbers
   (if any GPU shows `power.draw` N/A → it's a sliced card, re-rent).
2. **Start job stream**, then set **FIXED λ = 0.90** with the slider (at 10
   nodes, 0.85 packs onto 3 nodes below the setpoint; 0.90 packs 2 at full
   burn — the sharper pain act). Centrality-blind: burns
   land on the domain tier first; watch `b-*` watts jump to ~350–450 W and
   temps climb. This is real heat in a real datacenter.
3. **Kill → Save as ghost → Reset.**
4. **AUTO:** controller backs off as measured temps approach the setpoint;
   burns migrate off the domain (centrality on). NOTE: with continuous
   full-TDP burns, every active card runs hot — on real silicon the
   *placement* story (which nodes carry load) is the visual; tune
   `SETPOINT_C` to sit just under the observed plateau so the thermostat
   visibly holds. Duty-cycled burns are the finer knob for later rehearsal.
5. **ARM (A) → kill the domain:** `b-*` watts collapse to idle live; compare
   jobs-lost vs the ghost.
6. **Resilience bonus:** `tmux kill-session -t agent` on pod B → nodes go
   OFFLINE (gray) within ~6 s and the controller re-plans around them.
7. **Latency check:** dashboard over the proxy URL from a phone hotspot —
   the 1 Hz cadence should hold (short-polling, no long streams).

## Afterwards

- **Stop both pods** (billing stops).
- Screenshot the dashboard with 10 real nodes — deck/LinkedIn material.
- Note the observed plateau temps + heat-up times per card → set
  `SETPOINT_C` for the conference run.
