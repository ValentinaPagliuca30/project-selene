import json
import os
import sys
from datetime import datetime, timezone

import httpx


POD_PORT_RANGE = range(3001, 3013)
GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://localhost:3000")
OUTPUT_PATH = os.environ.get("MAP_OUTPUT_PATH", "/rover/output/map.json")
HTTP_TIMEOUT = 2.0

ENDPOINTS = ["info", "status", "dependencies", "supplies", "logs", "comms"]


def fetch_json(url):
    try:
        r = httpx.get(url, timeout=HTTP_TIMEOUT)
    except httpx.HTTPError:
        return None
    if r.status_code != 200:
        return None
    try:
        return r.json()
    except ValueError:
        return None


def find_pod_port(pod_id):
    for port in POD_PORT_RANGE:
        info = fetch_json(f"http://{pod_id}:{port}/info")
        if info and info.get("id") == pod_id:
            return port
    return None


def fetch_pod(pod_id, port):
    base = f"http://{pod_id}:{port}"
    return {ep: fetch_json(f"{base}/{ep}") for ep in ENDPOINTS}


def build_node(pod_id, port, raw):
    info = raw.get("info") or {}
    status = raw.get("status") or {}
    logs_resp = raw.get("logs")
    comms_resp = raw.get("comms") or {}

    if isinstance(logs_resp, list):
        logs = logs_resp
    elif isinstance(logs_resp, dict):
        logs = logs_resp.get("logs", [])
    else:
        logs = []

    comms = comms_resp.get("messages", []) if isinstance(comms_resp, dict) else []

    return {
        "id": pod_id,
        "name": info.get("name"),
        "role": info.get("role"),
        "population": info.get("population"),
        "uptime_days": info.get("uptime_days"),
        "status": status.get("status"),
        "alerts": status.get("alerts", []),
        "last_incident": status.get("last_incident"),
        "metadata": info.get("metadata", {}),
        "port": port,
        "raw": raw,
        "logs": logs,
        "comms": comms,
    }


def neighbors_of(node):
    raw = node.get("raw", {})
    deps = (raw.get("dependencies") or {}).get("dependencies") or []
    sups = (raw.get("supplies") or {}).get("supplies") or []
    return [d["pod_id"] for d in deps if "pod_id" in d] + \
           [s["pod_id"] for s in sups if "pod_id" in s]


def discover_and_crawl():
    gateway = fetch_json(GATEWAY_URL)
    if not gateway:
        sys.exit(f"gateway unreachable at {GATEWAY_URL}")
    entry = gateway.get("entrypoint", {}).get("pod")
    if not entry:
        sys.exit("gateway returned no entrypoint pod")

    nodes = {}
    queue = [entry]
    visited = set()

    while queue:
        pod_id = queue.pop(0)
        if pod_id in visited:
            continue
        visited.add(pod_id)

        port = find_pod_port(pod_id)
        if port is None:
            nodes[pod_id] = {"id": pod_id, "unreachable": True}
            continue

        raw = fetch_pod(pod_id, port)
        node = build_node(pod_id, port, raw)
        nodes[pod_id] = node

        for neighbor in neighbors_of(node):
            if neighbor not in visited:
                queue.append(neighbor)

    return nodes


def _new_edge(consumer, supplier, resource):
    return {
        "from": consumer,
        "to": supplier,
        "resource": resource,
        "criticality": None,
        "notes": None,
        "disputed": False,
        "claims": [],
    }


def build_edges(nodes):
    edges = {}

    for pod_id, node in nodes.items():
        if node.get("unreachable"):
            continue
        raw = node["raw"]

        deps = (raw.get("dependencies") or {}).get("dependencies") or []
        for d in deps:
            key = (pod_id, d["pod_id"], d["resource"])
            edge = edges.setdefault(key, _new_edge(*key))
            edge["claims"].append({
                "source": pod_id,
                "type": "dependency",
                "criticality": d.get("criticality"),
                "notes": d.get("notes"),
            })
            edge["criticality"] = d.get("criticality") or edge["criticality"]
            edge["notes"] = d.get("notes") or edge["notes"]

        sups = (raw.get("supplies") or {}).get("supplies") or []
        for s in sups:
            key = (s["pod_id"], pod_id, s["resource"])
            edge = edges.setdefault(key, _new_edge(*key))
            edge["claims"].append({
                "source": pod_id,
                "type": "supply",
            })

    out = []
    for edge in edges.values():
        types = {c["type"] for c in edge["claims"]}
        edge["disputed"] = not ("dependency" in types and "supply" in types)
        out.append(edge)
    return out


def find_discrepancies(edges):
    out = []
    for i, e in enumerate(edges):
        if not e["disputed"]:
            continue
        types = {c["type"] for c in e["claims"]}
        if types == {"dependency"}:
            kind = "consumer_only"
            desc = f"{e['from']} claims dependency on {e['to']} for {e['resource']}, but {e['to']} does not claim supplying it"
        elif types == {"supply"}:
            kind = "supplier_only"
            desc = f"{e['to']} claims supplying {e['resource']} to {e['from']}, but {e['from']} does not claim depending on it"
        else:
            kind = "unknown"
            desc = "unrecognized discrepancy pattern"
        out.append({
            "type": kind,
            "edge_index": i,
            "from": e["from"],
            "to": e["to"],
            "resource": e["resource"],
            "description": desc,
        })
    return out


def main():
    nodes = discover_and_crawl()
    edges = build_edges(nodes)
    discrepancies = find_discrepancies(edges)

    payload = {
        "metadata": {
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "gateway_url": GATEWAY_URL,
            "pod_count": sum(1 for n in nodes.values() if not n.get("unreachable")),
            "edge_count": len(edges),
            "discrepancy_count": len(discrepancies),
        },
        "nodes": nodes,
        "edges": edges,
        "discrepancies": discrepancies,
    }

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w") as f:
        json.dump(payload, f, indent=2)


if __name__ == "__main__":
    main()
