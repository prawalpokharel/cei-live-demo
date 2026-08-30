"""Live per-node CEI scoring + counterfactual ("what if this fails now?").

CEI = Criticality-Entropy Index. For the live surface we compute, per node,
a score in [0,1] that a heatmap can render and a what-if panel can explain.
Everything here is derived from MEASURED state (registry telemetry + real
job placement) — no modeled constants beyond the published weights.

Criticality  — how much this node concentrates that would be lost on failure:
  * centrality: membership in the shared high-centrality DOMAIN tier
  * load: fraction of its capacity currently running real jobs
  * dependency: whether other nodes' jobs depend on a service it hosts
Entropy      — how close it already is to trouble:
  * thermal headroom consumed toward the governance setpoint
  * degraded / cordoned condition
The blast radius is the concrete consequence: jobs that would be interrupted
and GPU-seconds that would be lost (progress since last checkpoint) if it
failed at this instant.
"""

# published weights (documented, not tuned per-run)
W_CENTRALITY = 0.40
W_LOAD = 0.25
W_THERMAL = 0.20
W_CONDITION = 0.15
PER_NODE_CAP = 4


def _thermal_stress(temp, setpoint, band):
    if temp <= band:
        return 0.0
    if temp >= setpoint:
        return 1.0
    return (temp - band) / max(1e-6, setpoint - band)


def node_scores(reg_snapshot, jobs_detail, setpoint, band, domain_service_nodes):
    """Return {node_id: {cei, criticality, entropy, blast_jobs,
    blast_gpu_s, in_domain, hosts_dependency}} for every fresh node.

    jobs_detail: {node_id: [{progress_s, ckpt_s}, ...]} of running jobs.
    domain_service_nodes: set of node ids that host a shared service other
      jobs depend on (experiment #2), amplifying criticality.
    """
    out = {}
    for n in reg_snapshot["nodes"]:
        if n.get("stale"):
            continue
        nid = n["id"]
        jobs = jobs_detail.get(nid, [])
        load = min(1.0, len(jobs) / PER_NODE_CAP)
        centrality = 1.0 if n.get("domain") else 0.30
        hosts_dep = nid in domain_service_nodes
        if hosts_dep:
            centrality = 1.0
        criticality = (W_CENTRALITY * centrality + W_LOAD * load) / (
            W_CENTRALITY + W_LOAD)
        thermal = _thermal_stress(n.get("temp", 0.0), setpoint, band)
        condition = 1.0 if (n.get("degraded") or n.get("cordoned")) else 0.0
        entropy = (W_THERMAL * thermal + W_CONDITION * condition) / (
            W_THERMAL + W_CONDITION)
        # CEI blends the two families; criticality dominates (a cool but
        # critical node still matters), entropy sharpens ties.
        cei = 0.70 * criticality + 0.30 * entropy
        blast_gpu_s = sum(max(0.0, j["progress_s"] - j["ckpt_s"]) for j in jobs)
        out[nid] = {
            "cei": round(cei, 3),
            "criticality": round(criticality, 3),
            "entropy": round(entropy, 3),
            "blast_jobs": len(jobs),
            "blast_gpu_s": round(blast_gpu_s, 1),
            "in_domain": bool(n.get("domain")),
            "hosts_dependency": hosts_dep,
            "thermal_stress": round(thermal, 3),
        }
    return out


def what_if(node_id, scores, jobs_detail, avg_recovery_s):
    """Counterfactual: predicted consequence of failing node_id right now,
    BEFORE it fails — the counterfactual-engine primitive."""
    s = scores.get(node_id)
    if s is None:
        return {"node": node_id, "known": False}
    jobs = jobs_detail.get(node_id, [])
    rec = avg_recovery_s if avg_recovery_s is not None else 1.0
    return {
        "node": node_id,
        "known": True,
        "cei": s["cei"],
        "predicted_jobs_interrupted": s["blast_jobs"],
        "predicted_gpu_seconds_lost": s["blast_gpu_s"],
        "predicted_recovery_s": round(rec * max(1, s["blast_jobs"]), 1),
        "in_domain": s["in_domain"],
        "hosts_dependency": s["hosts_dependency"],
        "rationale": _rationale(s),
    }


def _rationale(s):
    bits = []
    if s["in_domain"]:
        bits.append("shared high-centrality domain tier")
    if s["hosts_dependency"]:
        bits.append("hosts a service other jobs depend on")
    if s["blast_jobs"]:
        bits.append(f"{s['blast_jobs']} real jobs running on it now")
    if s["thermal_stress"] >= 0.5:
        bits.append("already thermally stressed")
    return "; ".join(bits) or "low criticality, low current load"
