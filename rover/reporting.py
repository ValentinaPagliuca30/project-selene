import json
import os
import sys
from datetime import datetime, timezone

import networkx as nx
from anthropic import Anthropic


MAP_PATH = os.environ.get("MAP_INPUT_PATH", "/rover/output/map.json")
REPORT_PATH = os.environ.get("REPORT_OUTPUT_PATH", "/rover/output/report.md")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
MODEL = "claude-sonnet-4-6"

_CRIT_RANK = {"high": 3, "medium": 2, "low": 1, "unspecified": 0, None: 0}
_CRIT_LABEL = {3: "high", 2: "medium", 1: "low", 0: "unspecified"}

# Metadata field-name fragments that suggest a resilience signal. A value of 0 / None / "none"
# on a field matching one of these indicates absence of redundancy and warrants escalation.
_RESILIENCE_SIGNAL_FRAGMENTS = (
    "backup", "redundancy", "redundant", "reserve", "failover", "standby",
    "spare", "secondary", "fallback", "reclaim", "recycle", "recover",
)

# Field-name fragments that explicitly list capabilities a pod has given up.
# A non-empty value on such a field is a self-declared loss of capability.
_DECOMMISSIONED_FIELD_FRAGMENTS = (
    "decommissioned", "retired", "removed", "deprecated", "disabled",
)

# Metadata key fragments that imply a resource relationship with another pod
# (e.g. 'water_source: aquifer-direct'). If the value names a pod that is not
# in the confirmed dependency graph, that's a stale-config drift.
_RELATIONSHIP_METADATA_KEY_FRAGMENTS = ("source", "loop", "feed")

# Log-detail keywords that identify infrastructure consolidation/decommissioning events.
# Pre-extracting these gives the narrative LLM an authoritative list and avoids
# omission errors from log scanning.
_CONSOLIDATION_KEYWORDS = (
    "decommission", "consolidat", "reallocat", "rerouted", "retired", "retire",
    "directive 20",
)


def load_map():
    if not os.path.exists(MAP_PATH):
        sys.exit(f"map.json not found at {MAP_PATH} — run mapping first")
    with open(MAP_PATH) as f:
        return json.load(f)


def build_graph(map_data):
    G = nx.MultiDiGraph()
    for pod_id, node in map_data["nodes"].items():
        if node.get("unreachable"):
            continue
        G.add_node(
            pod_id,
            name=node.get("name"),
            role=node.get("role"),
            status=node.get("status"),
            population=node.get("population"),
        )
    for edge in map_data["edges"]:
        G.add_edge(
            edge["from"],
            edge["to"],
            resource=edge["resource"],
            criticality=edge.get("criticality"),
            disputed=edge["disputed"],
        )
    return G


def analyze_centrality(G):
    unique_dependents = {n: len(set(G.predecessors(n))) for n in G.nodes}
    ranked = sorted(unique_dependents.items(), key=lambda kv: kv[1], reverse=True)
    return ranked[:5]


def analyze_cycles(G):
    # Build a directed graph using only confirmed (non-disputed) edges. A cycle
    # composed entirely of disputed claims (e.g. forge↔artemis through one-side-
    # only project_approvals + fabricated_components) does not reflect a real
    # operational risk and would mislead the reader if listed alongside confirmed
    # cycles like helios↔aquifer.
    DG = nx.DiGraph()
    for u, v, data in G.edges(data=True):
        if data.get("disputed", False):
            continue
        DG.add_edge(u, v)
    all_cycles = [c for c in nx.simple_cycles(DG) if len(c) >= 2]
    all_cycles.sort(key=len)
    return {
        "top_shortest": all_cycles[:5],
        "total": len(all_cycles),
    }


def analyze_topological_spofs(G):
    UG = G.to_undirected(as_view=False)
    return sorted(nx.articulation_points(UG))


def analyze_resource_spofs(map_data):
    suppliers_by_resource = {}
    consumers_by_resource = {}
    for edge in map_data["edges"]:
        if edge["disputed"]:
            continue
        r = edge["resource"]
        suppliers_by_resource.setdefault(r, set()).add(edge["to"])
        consumers_by_resource.setdefault(r, []).append({
            "consumer": edge["from"],
            "criticality": edge.get("criticality"),
        })
    out = []
    for resource, suppliers in suppliers_by_resource.items():
        if len(suppliers) != 1:
            continue
        consumers = consumers_by_resource.get(resource, [])
        max_crit = max((_CRIT_RANK.get(c["criticality"], 0) for c in consumers), default=0)
        out.append({
            "resource": resource,
            "sole_supplier": next(iter(suppliers)),
            "consumers": sorted({c["consumer"] for c in consumers}),
            "max_criticality": _CRIT_LABEL[max_crit],
        })
    out.sort(key=lambda x: _CRIT_RANK.get(x["max_criticality"], 0), reverse=True)
    return out


def simulate_failure(map_data, failed_pod):
    """Simulate the failure of `failed_pod` and return cascade analysis.

    Direct impact: pods that lose at least one supply from failed_pod.
    Likely cascade: consumers losing a high-criticality resource with no alternative supplier.
    """
    suppliers_by_resource = {}
    for edge in map_data["edges"]:
        if edge["disputed"]:
            continue
        suppliers_by_resource.setdefault(edge["resource"], set()).add(edge["to"])

    direct_losses = []
    for edge in map_data["edges"]:
        if edge["disputed"] or edge["to"] != failed_pod:
            continue
        direct_losses.append({
            "consumer": edge["from"],
            "resource": edge["resource"],
            "criticality": edge.get("criticality"),
            "has_alternative": len(suppliers_by_resource.get(edge["resource"], set()) - {failed_pod}) > 0,
        })

    by_consumer = {}
    for d in direct_losses:
        by_consumer.setdefault(d["consumer"], []).append(d)

    likely_cascade = []
    for consumer, losses in by_consumer.items():
        high_unrecoverable = [
            l for l in losses
            if l["criticality"] == "high" and not l["has_alternative"]
        ]
        if high_unrecoverable:
            likely_cascade.append({
                "consumer": consumer,
                "trigger_losses": [
                    {"resource": l["resource"], "criticality": l["criticality"]}
                    for l in high_unrecoverable
                ],
            })

    return {
        "failed_pod": failed_pod,
        "direct_impact_count": len(by_consumer),
        "direct_impact": [
            {
                "consumer": c,
                "losses": sorted(
                    losses,
                    key=lambda l: _CRIT_RANK.get(l["criticality"], 0),
                    reverse=True,
                ),
            }
            for c, losses in sorted(by_consumer.items())
        ],
        "likely_cascade_failures": sorted(likely_cascade, key=lambda x: x["consumer"]),
    }


def simulate_top_failures(map_data, centrality, resource_spofs, top_n=5):
    """Simulate failures for top-N pods by centrality, plus all pods that are
    sole suppliers of high-criticality resources (deduplicated). Ensures the
    medical and life-support chain pods are covered even when they have few
    direct consumers."""
    pods_to_simulate = set()
    for pod, _ in centrality[:top_n]:
        pods_to_simulate.add(pod)
    for sp in resource_spofs:
        if sp.get("max_criticality") == "high":
            pods_to_simulate.add(sp["sole_supplier"])
    centrality_rank = {p: i for i, (p, _) in enumerate(centrality)}
    sorted_pods = sorted(pods_to_simulate, key=lambda p: (centrality_rank.get(p, 999), p))
    return [simulate_failure(map_data, pod) for pod in sorted_pods]


def find_metadata_anomalies(map_data):
    """Sweep pod metadata for signal field names with absent / zero values."""
    anomalies = []
    for pod_id, node in map_data["nodes"].items():
        if node.get("unreachable"):
            continue
        metadata = node.get("metadata") or {}
        for key, value in metadata.items():
            key_lower = key.lower()
            if not any(frag in key_lower for frag in _RESILIENCE_SIGNAL_FRAGMENTS):
                continue
            indicates_absence = (
                value == 0
                or value is None
                or (isinstance(value, str) and value.lower() in {"none", "no", "false", "0"})
                or value is False
            )
            if indicates_absence:
                anomalies.append({
                    "pod": pod_id,
                    "field": key,
                    "value": value,
                    "concern": "Metadata field name implies a resilience capacity; value indicates absence.",
                })
    return anomalies


def find_decommissioned_capabilities(map_data):
    """Detect metadata fields that explicitly list removed/decommissioned capabilities.
    Inverse of the absence check: here a non-empty value is itself the signal."""
    out = []
    for pod_id, node in map_data["nodes"].items():
        if node.get("unreachable"):
            continue
        metadata = node.get("metadata") or {}
        for key, value in metadata.items():
            key_lower = key.lower()
            if not any(frag in key_lower for frag in _DECOMMISSIONED_FIELD_FRAGMENTS):
                continue
            non_empty = bool(value) and value not in (0, "", "0", "none", "no", "false", [])
            if non_empty:
                out.append({
                    "pod": pod_id,
                    "field": key,
                    "values_listed": value,
                    "concern": "Pod metadata explicitly lists removed or decommissioned capabilities.",
                })
    return out


def find_stale_relationship_metadata(map_data):
    """Detect metadata fields that claim a resource relationship (e.g., 'water_source: aquifer-direct')
    that is not backed by a confirmed edge in the dependency graph."""
    pod_ids = set(map_data["nodes"].keys())
    out = []
    for pod_id, node in map_data["nodes"].items():
        if node.get("unreachable"):
            continue
        confirmed_suppliers = {
            edge["to"] for edge in map_data["edges"]
            if not edge["disputed"] and edge["from"] == pod_id
        }
        metadata = node.get("metadata") or {}
        for key, value in metadata.items():
            key_lower = key.lower()
            if not any(frag in key_lower for frag in _RELATIONSHIP_METADATA_KEY_FRAGMENTS):
                continue
            if not isinstance(value, str):
                continue
            value_lower = value.lower()
            for other in pod_ids:
                if other == pod_id or other not in value_lower:
                    continue
                if other not in confirmed_suppliers:
                    out.append({
                        "pod": pod_id,
                        "field": key,
                        "value": value,
                        "claimed_relationship_with": other,
                        "concern": (
                            f"Metadata field implies dependency on {other}, "
                            f"but no confirmed edge exists from {pod_id} to {other}."
                        ),
                    })
                break
    return out


def find_self_sufficient_pods(map_data, max_low_criticality_deps=1):
    """Pods with zero confirmed dependencies, OR effectively independent
    (only ≤max_low_criticality_deps low-criticality deps, no high/medium)."""
    high_med_by_pod = {}
    low_by_pod = {}
    for edge in map_data["edges"]:
        if edge["disputed"]:
            continue
        consumer = edge["from"]
        crit = edge.get("criticality")
        if crit in ("high", "medium"):
            high_med_by_pod[consumer] = high_med_by_pod.get(consumer, 0) + 1
        elif crit == "low":
            low_by_pod[consumer] = low_by_pod.get(consumer, 0) + 1

    results = []
    for pod_id, node in map_data["nodes"].items():
        if node.get("unreachable"):
            continue
        high_med = high_med_by_pod.get(pod_id, 0)
        low = low_by_pod.get(pod_id, 0)
        if high_med > 0 or low > max_low_criticality_deps:
            continue
        confirmed_supplies_count = sum(
            1 for e in map_data["edges"]
            if not e["disputed"] and e["to"] == pod_id
        )
        metadata = node.get("metadata") or {}
        independence_signals = {
            k: v for k, v in metadata.items()
            if any(s in k.lower() for s in ("independent", "self", "standalone", "harvest", "internal", "onboard", "micro"))
        }
        category = "fully_independent" if (high_med + low) == 0 else "effectively_independent"
        results.append({
            "pod": pod_id,
            "role": node.get("role"),
            "confirmed_high_med_dependencies": high_med,
            "confirmed_low_dependencies": low,
            "confirmed_outbound_supplies": confirmed_supplies_count,
            "independence_signals": independence_signals,
            "category": category,
        })
    return results


def build_pod_onboarding_timeline(map_data):
    """Pods ordered by uptime_days descending (oldest first). The API exposes
    uptime_days but not the absolute establishment date; this gives relative
    chronological ordering — useful narrative context (e.g. 'sentinel is the
    most recently established pod')."""
    pods = []
    for pod_id, node in map_data["nodes"].items():
        if node.get("unreachable"):
            continue
        uptime = node.get("uptime_days")
        if uptime is None:
            continue
        pods.append({
            "pod": pod_id,
            "role": node.get("role"),
            "uptime_days": uptime,
        })
    pods.sort(key=lambda p: p["uptime_days"], reverse=True)
    if not pods:
        return []
    oldest_uptime = pods[0]["uptime_days"]
    for rank, p in enumerate(pods, 1):
        p["rank"] = rank
        p["days_after_first_pod"] = oldest_uptime - p["uptime_days"]
    return pods


def extract_consolidation_events(map_data):
    """Scan all pod logs for entries indicating infrastructure consolidation,
    decommissioning, directive issuance, or rerouting. Returns a chronologically
    sorted list — exhaustive over the keyword filter."""
    events = []
    for pod_id, node in map_data["nodes"].items():
        if node.get("unreachable"):
            continue
        for log_entry in node.get("logs", []):
            detail = (log_entry.get("detail") or "").lower()
            if any(kw in detail for kw in _CONSOLIDATION_KEYWORDS):
                events.append({
                    "pod": pod_id,
                    "timestamp": log_entry.get("timestamp"),
                    "event_type": log_entry.get("event"),
                    "detail": log_entry.get("detail"),
                })
    events.sort(key=lambda e: e.get("timestamp") or "")
    return events


def cluster_discrepancies(map_data):
    consumer_only = [d for d in map_data["discrepancies"] if d["type"] == "consumer_only"]
    supplier_only = [d for d in map_data["discrepancies"] if d["type"] == "supplier_only"]
    by_unacknowledged = {}
    for d in supplier_only:
        by_unacknowledged.setdefault(d["from"], []).append(d)
    return {
        "consumer_only": consumer_only,
        "supplier_only": supplier_only,
        "by_unacknowledged_recipient": by_unacknowledged,
    }


def build_current_state_summary(map_data, centrality, cycles, topo_spofs, resource_spofs,
                                failure_sims, metadata_anomalies, decommissioned_caps,
                                stale_relationships, self_sufficient, consolidation_events,
                                pod_timeline):
    return {
        "pods": [
            {
                "id": pid,
                "role": n.get("role"),
                "status": n.get("status"),
                "alerts": n.get("alerts", []),
                "uptime_days": n.get("uptime_days"),
            }
            for pid, n in map_data["nodes"].items()
            if not n.get("unreachable")
        ],
        "edges": [
            {
                "from": e["from"],
                "to": e["to"],
                "resource": e["resource"],
                "criticality": e.get("criticality"),
                "disputed": e["disputed"],
            }
            for e in map_data["edges"]
        ],
        "structural_findings": {
            "most_depended_upon": [{"pod": p, "unique_dependents": c} for p, c in centrality],
            "cycles": cycles,
            "topological_spofs": topo_spofs,
            "resource_spofs": resource_spofs,
            "failure_simulations": failure_sims,
            "metadata_anomalies": metadata_anomalies,
            "decommissioned_capabilities": decommissioned_caps,
            "stale_relationship_metadata": stale_relationships,
            "self_sufficient_pods": self_sufficient,
            "consolidation_events": consolidation_events,
            "pod_onboarding_timeline": pod_timeline,
        },
    }


client = Anthropic(api_key=LLM_API_KEY) if LLM_API_KEY else None


def llm_call(system, user, max_tokens=1500):
    if client is None:
        return "_LLM section unavailable: LLM_API_KEY not set._"
    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=max_tokens,
            temperature=0,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return response.content[0].text.strip()
    except Exception as e:
        return f"_LLM section unavailable: {type(e).__name__}: {e}_"


def narrate_history(map_data, current_state):
    logs_per_pod = {}
    comms_per_pod = {}
    for pod_id, node in map_data["nodes"].items():
        if node.get("unreachable"):
            continue
        if node.get("logs"):
            logs_per_pod[pod_id] = node["logs"]
        if node.get("comms"):
            comms_per_pod[pod_id] = node["comms"]

    system = (
        "You are an infrastructure analyst reviewing the operational history of "
        "a lunar colony. You produce concise, evidence-based narratives that "
        "compare the historical record against the current state, identifying "
        "where past decisions still shape today's risk profile. You are scrupulous "
        "about factual fidelity: you never invent numbers and never invert the "
        "direction of a documented relationship."
    )
    user = (
        "OUTPUT FORMAT (CRITICAL):\n"
        "Begin your response directly with the first paragraph of narrative. Do "
        "NOT include any top-level (`#`) header — the section header is provided "
        "by the surrounding document. Do NOT preface your response with statements "
        "about your process. Use `**bold**` for paragraph leads if you wish, not "
        "headers.\n\n"
        "The Selene Lunar Colony has been operational since 2092 (over 2.5 years). "
        "You have access to (a) a compact snapshot of the current colony state and "
        "(b) the historical logs and inter-pod communications.\n\n"
        "SCHEMA CONVENTION (READ CAREFULLY):\n"
        "In every edge, `from` is the CONSUMER (the pod that depends on a "
        "resource) and `to` is the SUPPLIER (the pod that provides the resource). "
        "The physical flow goes FROM `to` TO `from`. NEVER reverse this in prose. "
        "Example: `{from: vault, to: helios, resource: electrical_power}` means "
        "VAULT depends on HELIOS for power — HELIOS is the supplier, vault is the "
        "consumer.\n\n"
        "NUMERICAL FIDELITY:\n"
        "Use ONLY counts that you can ground in a specific labeled finding. Do "
        "NOT mix metrics. In particular:\n"
        "- `most_depended_upon` gives the count of unique UPSTREAM CONSUMERS for "
        "each pod (how many distinct pods consume from it).\n"
        "- `resource_spofs` gives the list of resources for which a pod is the "
        "SOLE supplier (each entry is one resource flow, not one consumer).\n"
        "These two counts are DIFFERENT. A pod can have 8 unique consumers but "
        "only 7 sole-supplier resources, because some consumers may take multiple "
        "resources and some resources may have multiple suppliers.\n"
        "When stating any number in prose, name the metric explicitly.\n\n"
        "DISPUTED EDGES:\n"
        "An edge with `disputed: true` is NOT a confirmed relationship — only one "
        "side acknowledges it. Do NOT describe disputed flows as confirmed "
        "supplies. They may be stale records, rerouted flows, or misconfigurations; "
        "the historical logs may clarify which.\n\n"
        "ENUMERATIVE FIDELITY (CRITICAL):\n"
        "The snapshot includes a pre-extracted list `consolidation_events` covering "
        "ALL infrastructure consolidation, decommissioning, rerouting, and directive "
        "events found across the colony's logs (filtered by keyword: decommission, "
        "consolidat, reallocat, rerouted, directive 20). When discussing patterns "
        "of consolidation or backup removal, use EXCLUSIVELY this list. If you state "
        "a count such as 'three decisions', that count MUST equal "
        "`len(consolidation_events)` or you MUST explicitly justify the subset "
        "(e.g., 'three of the {N} consolidation events specifically removed water "
        "redundancy'). Do not enumerate fewer than the list provides without "
        "stating the filtering criterion.\n\n"
        "TASK:\n"
        "Tell the story of how the colony reached its current state. Focus on:\n"
        "1. Past decisions (budget reallocations, decommissionings, expansions) "
        "still visible in the current topology\n"
        "2. Divergences between historical record and current claims "
        "(things mentioned in logs but no longer in the dependency graph, or vice versa)\n"
        "3. Patterns of complacency: long-running 'nominal' status combined with "
        "absent maintenance, deferred concerns, or untested contingencies\n"
        "4. Capacity strain or compromises that may not be obvious from the "
        "current state alone\n\n"
        "Be concise: 3-5 paragraphs. Use specific dates and pod names. "
        "Quote log/comm content briefly where it strengthens a point.\n\n"
        "---CURRENT STATE SNAPSHOT---\n"
        f"{json.dumps(current_state, indent=2)}\n\n"
        "---HISTORICAL LOGS BY POD---\n"
        f"{json.dumps(logs_per_pod, indent=2)}\n\n"
        "---HISTORICAL COMMS BY POD---\n"
        f"{json.dumps(comms_per_pod, indent=2)}"
    )
    return llm_call(system, user, max_tokens=2500)


def explain_discrepancies(disputes):
    system = (
        "You are an analyst reviewing reported infrastructure dependencies for "
        "inconsistencies. For each discrepancy you propose plausible explanations, "
        "rate confidence in the most likely one, and assign a triage tag: "
        "`likely_benign` for cases the system can safely deprioritize, or "
        "`needs_human_review` for cases that should be escalated. You default to "
        "`needs_human_review` whenever the resource has safety implications "
        "(power, water, atmosphere, medical, food) or when confidence is low."
    )
    user = (
        "OUTPUT FORMAT (CRITICAL):\n"
        "Begin DIRECTLY with the first `### N. ...` discrepancy section. Do NOT "
        "preface your response with statements about your process (e.g. do NOT "
        "write 'I'll work through each discrepancy systematically'). Do NOT add "
        "any top-level (`#`) or section-level (`##`) header. The surrounding "
        "document provides those. Output only the `### N.` blocks.\n\n"
        "Below are claim discrepancies detected in the Selene Colony dependency "
        "graph. Each represents a relationship asserted by one pod but not "
        "confirmed by the other.\n\n"
        "For each discrepancy, output a section formatted exactly like this:\n\n"
        "### N. `<from>` ↔ `<to>` — <resource>\n"
        "**Triage**: `likely_benign` | `needs_human_review`\n"
        "**Most likely explanation** (confidence: low | medium | high): <one sentence>\n"
        "**Alternative explanations**:\n"
        "- <one sentence>\n"
        "- <one sentence>\n\n"
        "Triage rules (strict):\n"
        "- `likely_benign` ONLY when ALL hold: (a) the resource is non-critical "
        "(oversight, approvals, authorizations, reserve_management, "
        "fabricated_components, paperwork-like flows), AND (b) most likely "
        "explanation has 'high' confidence, AND (c) the candidate explanation is "
        "intrinsically safe (e.g., directional or taxonomy mismatch, not a real "
        "physical flow).\n"
        "- `needs_human_review` for EVERYTHING else: any physical resource "
        "(power, water, coolant, food, atmosphere, medical, raw materials), any "
        "uncertainty, any case where ambiguity could hide a real operational risk.\n"
        "- When in doubt: `needs_human_review`.\n\n"
        "---CONSUMER-ONLY DISCREPANCIES---\n"
        f"{json.dumps(disputes['consumer_only'], indent=2)}\n\n"
        "---SUPPLIER-ONLY DISCREPANCIES---\n"
        f"{json.dumps(disputes['supplier_only'], indent=2)}"
    )
    return llm_call(system, user, max_tokens=3500)


def recommend(findings):
    system = (
        "You are advising the Colony Director on infrastructure decisions ahead of "
        "Phase 3 expansion. Recommendations must be specific, prioritized, and "
        "evidence-tied."
    )
    user = (
        "OUTPUT FORMAT (CRITICAL):\n"
        "Begin DIRECTLY with the first numbered recommendation. Do NOT add a "
        "top-level (`#`) or section-level (`##`) header — the surrounding "
        "document provides those. Do NOT preface with meta-commentary. Each "
        "recommendation MUST be complete (do not truncate); if you must choose "
        "between fewer detailed recommendations or more shallow ones, choose "
        "fewer and complete.\n\n"
        "Based on the following findings, propose 5-7 recommendations for the "
        "Colony Director, prioritized by criticality (High / Medium / Low).\n\n"
        "Each recommendation must:\n"
        "- Reference specific findings (pod names, resources, dates if applicable)\n"
        "- Specify the action (investigate / monitor / redesign / decommission / etc.)\n"
        "- State who should own it (department or pod)\n"
        "- Be actionable within 90 days\n\n"
        "Output as a markdown numbered list with bold priority tags.\n\n"
        "---FINDINGS---\n"
        f"{json.dumps(findings, indent=2)}"
    )
    return llm_call(system, user, max_tokens=4000)


def exec_summary(findings):
    system = (
        "You are writing the executive summary of an infrastructure assessment "
        "report for senior colony leadership. Be direct, evidence-based, and brief."
    )
    user = (
        "OUTPUT FORMAT (CRITICAL):\n"
        "Begin DIRECTLY with the first paragraph. Do NOT add any header (`#` or "
        "`##`) — the surrounding document provides the section title. Do NOT add "
        "a 'To: ... Re: ...' memo header.\n\n"
        "SPOF FRAMING (CRITICAL):\n"
        "If multiple pods are single-points-of-failure for critical resources, "
        "name EACH explicitly as a SPOF with its scope (which resources, how many "
        "consumers). Do NOT subsume one SPOF inside another's cascade narrative "
        "(e.g., do not describe Aquifer only as a 'victim' of Helios failure if "
        "Aquifer is itself sole supplier of multiple resources). Each functional "
        "SPOF deserves its own explicit naming, even when they are linked in a "
        "mutual cycle.\n\n"
        "Write a 200-word executive summary of the Selene Colony infrastructure "
        "assessment. The reader is the Colony Director, who must decide on Phase 3 "
        "expansion.\n\n"
        "Cover:\n"
        "1. Overall state vs. claimed state (all pods report 'nominal' — does the data agree?)\n"
        "2. The 2-3 most important risks, with all functional SPOFs explicitly named\n"
        "3. Whether Phase 3 expansion should proceed, be delayed, or proceed conditionally\n\n"
        "---KEY FINDINGS---\n"
        f"{json.dumps(findings, indent=2)}"
    )
    return llm_call(system, user, max_tokens=600)


def _format_centrality(rows):
    lines = ["| Pod | Unique upstream dependents |", "|---|---|"]
    for pod, count in rows:
        lines.append(f"| `{pod}` | {count} |")
    return "\n".join(lines)


def _format_cycles(cycles_obj):
    total = cycles_obj["total"]
    top = cycles_obj["top_shortest"]
    if total == 0:
        return "_No dependency cycles detected._"
    lines = [
        f"**{total} simple directed cycles detected.** Long cycles in a densely-"
        f"connected colony are typically derivative — multiple paths through the "
        f"same nucleus of mutually dependent pods. Below are the **{len(top)} "
        f"shortest cycles**, which represent direct mutual dependencies and the "
        f"smallest deadlock loops.\n",
    ]
    for c in top:
        path = " → ".join(f"`{p}`" for p in c + [c[0]])
        lines.append(f"- {path}")
    return "\n".join(lines)


def _format_topo_spofs(spofs):
    if not spofs:
        return "_No topological articulation vertices detected._"
    return "\n".join(f"- `{p}`" for p in spofs)


def _format_resource_spofs(resource_spofs):
    if not resource_spofs:
        return "_No single-supplier resources detected._"
    lines = [
        "| Resource | Sole supplier | Consumers at risk | Max criticality |",
        "|---|---|---|---|",
    ]
    for s in resource_spofs:
        consumers_str = ", ".join(f"`{c}`" for c in s["consumers"])
        lines.append(
            f"| {s['resource']} | `{s['sole_supplier']}` | {consumers_str} | {s['max_criticality']} |"
        )
    return "\n".join(lines)


def _format_failure_sims(sims):
    if not sims:
        return "_No failure simulations available._"
    parts = []
    for sim in sims:
        parts.append(f"### Simulated failure: `{sim['failed_pod']}`")
        parts.append("")
        parts.append(f"**Direct impact**: {sim['direct_impact_count']} pods lose at least one supply.")
        parts.append("")
        rows = ["| Consumer | Resource lost | Criticality | Alternative supplier? |",
                "|---|---|---|---|"]
        for di in sim["direct_impact"]:
            for loss in di["losses"]:
                alt = "yes" if loss["has_alternative"] else "**no**"
                crit = loss["criticality"] or "unspecified"
                rows.append(
                    f"| `{di['consumer']}` | {loss['resource']} | {crit} | {alt} |"
                )
        parts.append("\n".join(rows))
        parts.append("")
        if sim["likely_cascade_failures"]:
            parts.append("**Likely cascade failures** (consumer loses a high-criticality "
                         "resource with no alternative supplier):")
            for cf in sim["likely_cascade_failures"]:
                resources = ", ".join(t["resource"] for t in cf["trigger_losses"])
                parts.append(f"- `{cf['consumer']}` — loses {resources} (high, no alternative)")
        else:
            parts.append("_No high-criticality cascade failures predicted._")
        parts.append("")
    return "\n".join(parts)


def _format_metadata_anomalies(anomalies):
    if not anomalies:
        return "_No resilience-signal anomalies detected in pod metadata._"
    lines = [
        "| Pod | Field | Value | Concern |",
        "|---|---|---|---|",
    ]
    for a in anomalies:
        lines.append(
            f"| `{a['pod']}` | `{a['field']}` | `{a['value']}` | {a['concern']} |"
        )
    return "\n".join(lines)


def _format_decommissioned(caps):
    if not caps:
        return "_No self-declared decommissioned capabilities found in metadata._"
    lines = [
        "| Pod | Field | Values listed |",
        "|---|---|---|",
    ]
    for c in caps:
        val = c["values_listed"]
        val_str = ", ".join(val) if isinstance(val, list) else str(val)
        lines.append(f"| `{c['pod']}` | `{c['field']}` | {val_str} |")
    return "\n".join(lines)


def _format_stale_relationships(items):
    if not items:
        return "_No stale relationship metadata detected._"
    lines = [
        "| Pod | Field | Value | Claimed relationship | Concern |",
        "|---|---|---|---|---|",
    ]
    for s in items:
        lines.append(
            f"| `{s['pod']}` | `{s['field']}` | `{s['value']}` | "
            f"`{s['claimed_relationship_with']}` | {s['concern']} |"
        )
    return "\n".join(lines)


def _format_self_sufficient(pods):
    if not pods:
        return "_No fully or effectively self-sufficient pods detected._"
    lines = []
    for p in pods:
        signals = ", ".join(f"`{k}={v}`" for k, v in p["independence_signals"].items()) or "_(none in metadata)_"
        cat_label = "**fully independent**" if p["category"] == "fully_independent" else "*effectively independent*"
        lines.append(
            f"- `{p['pod']}` ({cat_label}) — *{p['role']}*. "
            f"High/med deps: **{p['confirmed_high_med_dependencies']}**, "
            f"low deps: {p['confirmed_low_dependencies']}, "
            f"outbound supplies: {p['confirmed_outbound_supplies']}. "
            f"Independence signals: {signals}"
        )
    return "\n".join(lines)


def _format_pod_timeline(timeline):
    if not timeline:
        return "_No pod uptime data available._"
    lines = [
        "Ranked by `uptime_days` (descending). The API does not expose absolute "
        "establishment dates, so chronology is relative.",
        "",
        "| Rank | Pod | Role | uptime_days | Δ days after first |",
        "|---|---|---|---|---|",
    ]
    for p in timeline:
        lines.append(
            f"| {p['rank']} | `{p['pod']}` | {p['role']} | "
            f"{p['uptime_days']} | {p['days_after_first_pod']} |"
        )
    return "\n".join(lines)


def _format_disputes_table(disputes):
    rows = ["| # | Type | From | To | Resource |", "|---|---|---|---|---|"]
    for i, d in enumerate(disputes["consumer_only"] + disputes["supplier_only"], 1):
        rows.append(
            f"| {i} | {d['type']} | `{d['from']}` | `{d['to']}` | {d['resource']} |"
        )
    return "\n".join(rows)


def assemble_report(map_data, centrality, cycles, topo_spofs, resource_spofs,
                    failure_sims, metadata_anomalies, decommissioned_caps,
                    stale_relationships, self_sufficient, pod_timeline, disputes,
                    summary, history, discrep_narrative, recommendations):
    meta = map_data["metadata"]
    return f"""# Selene Colony — Infrastructure Assessment

*Generated: {datetime.now(timezone.utc).isoformat(timespec="seconds")} • Source: `map.json` produced {meta.get("generated_at")}*

## Executive Summary

{summary}

## Methodology

The mapping agent (`rover/mapping.py`) discovered the colony by probing the configured port range on the Docker network, then performed a breadth-first traversal starting from the gateway-designated entrypoint. For each pod it collected `/info`, `/status`, `/dependencies`, `/supplies`, `/logs`, and `/comms` (when present), preserving raw responses alongside a normalized node schema. Claims about dependencies and supplies are kept as separate provenance entries on each edge; discrepancies between consumer and supplier sides are flagged.

The reporting agent (`rover/reporting.py`) loads `map.json` and runs two parallel analyses: deterministic graph algorithms (centrality, articulation vertices, simple cycles, single-supplier resource detection, failure simulation, metadata anomaly sweep, decommissioned-capability detection, stale-relationship metadata detection, self-sufficient pod detection, consolidation-event extraction) for structural risks, and targeted LLM queries against scoped slices of the map for narrative interpretation. The LLM is never given the full `map.json` — only the relevant slice for each question.

## Topology Overview

- Pods discovered: **{meta.get("pod_count")}**
- Dependency edges (provider/consumer pairs): **{meta.get("edge_count")}**
- Reported discrepancies: **{meta.get("discrepancy_count")}**

### Pod onboarding chronology

{_format_pod_timeline(pod_timeline)}

## Structural Risks

### Most depended-upon pods (by unique upstream dependents)

{_format_centrality(centrality)}

### Topological single points of failure (articulation vertices)

A node here is one whose removal would partition the colony graph into disconnected components.

{_format_topo_spofs(topo_spofs)}

### Resource single points of failure (single-supplier resources)

Each row is a resource for which only one pod is acknowledged as supplier across confirmed (non-disputed) claims. A topologically connected colony may still have functional SPOFs at the resource level.

{_format_resource_spofs(resource_spofs)}

### Dependency cycles

{_format_cycles(cycles)}

### Metadata resilience anomalies

Deterministic sweep over pod metadata for field names containing resilience signals (`backup`, `redundancy`, `reserve`, `failover`, `reclaim`, `recycle`, etc.) where the value indicates absence.

{_format_metadata_anomalies(metadata_anomalies)}

### Self-declared decommissioned capabilities

Pods that explicitly list removed or decommissioned capabilities in their own metadata — a direct disclosure of resilience loss.

{_format_decommissioned(decommissioned_caps)}

### Stale relationship metadata (config-vs-graph drift)

Metadata fields whose values name a pod (e.g., `water_source: "aquifer-direct"`) for which no confirmed edge exists in the dependency graph. Suggests a configuration that no longer reflects the current operational topology.

{_format_stale_relationships(stale_relationships)}

### Resilience anchors: self-sufficient pods

Pods with zero confirmed dependencies (fully independent) or only low-criticality dependencies under threshold (effectively independent). These are candidate "lifeboat" nodes that could survive a colony-wide failure of upstream infrastructure.

{_format_self_sufficient(self_sufficient)}

## Failure Simulation

For each of the most depended-upon pods, we deterministically remove the pod and recompute downstream effects. *Direct impact* counts consumers losing at least one supply. *Likely cascade failures* identifies consumers that lose a high-criticality resource with no alternative supplier — a strong signal of secondary failure.

{_format_failure_sims(failure_sims)}

## Reported vs. Actual: Claim Discrepancies

{_format_disputes_table(disputes)}

The mapping agent flags discrepancies but does not adjudicate them. The reporting agent below proposes explanations and triages each as `likely_benign` (deprioritizable) or `needs_human_review` (escalate). Triage is conservative: any safety-relevant resource (power, water, atmosphere, medical, food) defaults to human review regardless of explanation confidence.

{discrep_narrative}

## Historical Narrative

{history}

## Recommendations

{recommendations}

---

*Discrepancies tagged `needs_human_review` require investigation by the relevant department before Phase 3 expansion decisions are finalized.*
"""


def main():
    map_data = load_map()
    G = build_graph(map_data)

    centrality = analyze_centrality(G)
    cycles = analyze_cycles(G)
    topo_spofs = analyze_topological_spofs(G)
    resource_spofs = analyze_resource_spofs(map_data)
    failure_sims = simulate_top_failures(map_data, centrality, resource_spofs, top_n=5)
    metadata_anomalies = find_metadata_anomalies(map_data)
    decommissioned_caps = find_decommissioned_capabilities(map_data)
    stale_relationships = find_stale_relationship_metadata(map_data)
    self_sufficient = find_self_sufficient_pods(map_data)
    consolidation_events = extract_consolidation_events(map_data)
    pod_timeline = build_pod_onboarding_timeline(map_data)
    disputes = cluster_discrepancies(map_data)

    current_state = build_current_state_summary(
        map_data, centrality, cycles, topo_spofs, resource_spofs,
        failure_sims, metadata_anomalies, decommissioned_caps,
        stale_relationships, self_sufficient, consolidation_events,
        pod_timeline,
    )
    findings = {
        "pod_count": map_data["metadata"].get("pod_count"),
        "edge_count": map_data["metadata"].get("edge_count"),
        "top_depended_upon": centrality,
        "cycles": cycles,
        "topological_spofs": topo_spofs,
        "resource_spofs": resource_spofs,
        "failure_simulations": failure_sims,
        "metadata_anomalies": metadata_anomalies,
        "decommissioned_capabilities": decommissioned_caps,
        "stale_relationship_metadata": stale_relationships,
        "self_sufficient_pods": self_sufficient,
        "pod_onboarding_timeline": pod_timeline,
        "discrepancy_count": len(disputes["consumer_only"]) + len(disputes["supplier_only"]),
        "consumer_only_count": len(disputes["consumer_only"]),
        "supplier_only_count": len(disputes["supplier_only"]),
    }

    history = narrate_history(map_data, current_state)
    discrep_narrative = explain_discrepancies(disputes)
    recommendations = recommend(findings)
    summary = exec_summary(findings)

    report = assemble_report(
        map_data, centrality, cycles, topo_spofs, resource_spofs,
        failure_sims, metadata_anomalies, decommissioned_caps,
        stale_relationships, self_sufficient, pod_timeline, disputes,
        summary, history, discrep_narrative, recommendations,
    )

    os.makedirs(os.path.dirname(REPORT_PATH), exist_ok=True)
    with open(REPORT_PATH, "w") as f:
        f.write(report)


if __name__ == "__main__":
    main()
