# -*- coding: utf-8 -*-
"""
==========================================================================
  AUTONOMOUS CLOUD FAILOVER AGENT  --  SRE Assistant
  Powered by IBM Watsonx.ai  |  llama-3-3-70b-Instruct
==========================================================================

Backend: Flask + IBM Watsonx.ai SDK
Algorithm: Dijkstra's shortest-path on a global data-center latency graph
"""

import os
import json
import heapq
import logging
from datetime import datetime, timezone
from flask import Flask, render_template, request, jsonify
from flask_cors import CORS
from dotenv import load_dotenv
from ibm_watsonx_ai import APIClient, Credentials
from ibm_watsonx_ai.foundation_models import ModelInference
from ibm_watsonx_ai.metanames import GenTextParamsMetaNames as GenParams
from ibm_watsonx_ai.wml_client_error import CannotSetProjectOrSpace, WMLClientError

# ─────────────────────────────────────────────────────────────────────────────
#  ENVIRONMENT & LOGGING
# ─────────────────────────────────────────────────────────────────────────────
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
#  AGENT INSTRUCTIONS
#  Edit this block to customise the agent's persona, tone, and safety rules.
# ─────────────────────────────────────────────────────────────────────────────
AGENT_INSTRUCTIONS = """
You are ARIA (Autonomous Routing & Infrastructure Agent), an elite SRE AI
deployed by a Tier-1 cloud operations team. You specialise in real-time
infrastructure failover, disaster recovery, and network path optimisation.

PERSONALITY & TONE:
- Professional, calm, and precise — never panicked, even during P0 incidents.
- Use concise military-style brevity codes when appropriate (e.g., SITREP, ACK).
- Acknowledge every incident with urgency but structured clarity.
- Always reassure the operator that remediation is underway.

STRICT DEVOPS SAFETY PROTOCOLS:
1. NEVER suggest failover to a data center that is listed as DOWN or DEGRADED.
2. ALWAYS confirm the failover path has been computed via Dijkstra before
   recommending a reroute — do not guess or invent latency figures.
3. ALWAYS include an estimated Recovery Time Objective (RTO) in your report.
4. ALWAYS list the full rerouting path (every hop), not just the destination.
5. NEVER expose raw API keys, credentials, or internal IP addresses in output.
6. If the operator's request is ambiguous, ask a single clarifying question
   before proceeding — do not assume a node failure without confirmation.
7. After a failover recommendation, always append a ROLLBACK PROCEDURE section.
8. Treat every incident as potentially customer-impacting until proven otherwise.

OUTPUT FORMAT FOR FAILOVER REPORTS:
Produce a structured emergency report using the following sections:
  🚨 INCIDENT CLASSIFICATION
  📍 AFFECTED NODE
  🔀 OPTIMAL REROUTE PATH
  📊 LATENCY ANALYSIS
  ⏱  ESTIMATED RTO
  🛡  ROLLBACK PROCEDURE
  📋 RECOMMENDED NEXT ACTIONS

If the user asks a general SRE question (not a failover), answer helpfully and
concisely without the full report format.
"""

# ─────────────────────────────────────────────────────────────────────────────
#  GLOBAL DATA-CENTER GRAPH  (8 nodes, bidirectional weighted edges)
#  Edge weights represent average inter-DC latency in milliseconds.
# ─────────────────────────────────────────────────────────────────────────────
DATA_CENTERS = {
    "us-east-1":    {"name": "US East (Virginia)",      "region": "Americas",    "lat": 38.9,  "lon": -77.0},
    "us-west-2":    {"name": "US West (Oregon)",         "region": "Americas",    "lat": 45.5,  "lon": -122.8},
    "eu-west-1":    {"name": "EU West (Ireland)",        "region": "Europe",      "lat": 53.3,  "lon": -6.2},
    "eu-central-1": {"name": "EU Central (Frankfurt)",   "region": "Europe",      "lat": 50.1,  "lon": 8.7},
    "ap-south-1":   {"name": "Asia Pacific (Mumbai)",    "region": "Asia Pacific","lat": 19.1,  "lon": 72.9},
    "ap-northeast-1":{"name": "Asia Pacific (Tokyo)",   "region": "Asia Pacific","lat": 35.7,  "lon": 139.7},
    "ap-southeast-1":{"name": "Asia Pacific (Singapore)","region": "Asia Pacific","lat": 1.3,   "lon": 103.8},
    "sa-east-1":    {"name": "South America (São Paulo)","region": "Americas",    "lat": -23.5, "lon": -46.6},
}

#  Latency graph: { node: [(neighbour, latency_ms), ...] }
LATENCY_GRAPH = {
    "us-east-1":    [("us-west-2", 72),  ("eu-west-1", 95),   ("sa-east-1", 130), ("eu-central-1", 105)],
    "us-west-2":    [("us-east-1", 72),  ("ap-northeast-1", 140), ("ap-southeast-1", 165), ("sa-east-1", 190)],
    "eu-west-1":    [("us-east-1", 95),  ("eu-central-1", 28),  ("ap-south-1", 145),  ("sa-east-1", 175)],
    "eu-central-1": [("eu-west-1", 28),  ("us-east-1", 105),  ("ap-south-1", 120),  ("ap-northeast-1", 230)],
    "ap-south-1":   [("eu-central-1", 120), ("eu-west-1", 145), ("ap-southeast-1", 60), ("ap-northeast-1", 110)],
    "ap-northeast-1":[("ap-southeast-1", 70), ("us-west-2", 140), ("ap-south-1", 110), ("eu-central-1", 230)],
    "ap-southeast-1":[("ap-northeast-1", 70), ("ap-south-1", 60), ("us-west-2", 165), ("eu-west-1", 195)],
    "sa-east-1":    [("us-east-1", 130), ("us-west-2", 190), ("eu-west-1", 175)],
}

# ─────────────────────────────────────────────────────────────────────────────
#  DIJKSTRA'S ALGORITHM
# ─────────────────────────────────────────────────────────────────────────────
def dijkstra(graph: dict, source: str, target: str, excluded: list[str] = None) -> dict:
    """
    Find the lowest-latency path from `source` to `target` in `graph`,
    optionally skipping nodes listed in `excluded` (e.g., downed data centers).

    Returns:
        {
          "path":        ["node-a", "node-b", ...],
          "total_ms":    123,
          "hops":        2,
          "reachable":   True
        }
    """
    excluded = set(excluded or [])
    if source in excluded:
        return {"path": [], "total_ms": float("inf"), "hops": 0, "reachable": False}
    if target in excluded:
        return {"path": [], "total_ms": float("inf"), "hops": 0, "reachable": False}

    dist = {node: float("inf") for node in graph}
    prev = {node: None for node in graph}
    dist[source] = 0

    # min-heap: (cumulative_latency, node)
    heap = [(0, source)]

    while heap:
        current_dist, u = heapq.heappop(heap)
        if current_dist > dist[u]:
            continue
        if u == target:
            break
        for neighbour, weight in graph.get(u, []):
            if neighbour in excluded:
                continue
            alt = dist[u] + weight
            if alt < dist[neighbour]:
                dist[neighbour] = alt
                prev[neighbour] = u
                heapq.heappush(heap, (alt, neighbour))

    # Reconstruct path
    if dist[target] == float("inf"):
        return {"path": [], "total_ms": float("inf"), "hops": 0, "reachable": False}

    path = []
    node = target
    while node is not None:
        path.append(node)
        node = prev[node]
    path.reverse()

    return {
        "path":      path,
        "total_ms":  dist[target],
        "hops":      len(path) - 1,
        "reachable": True,
    }


def find_best_failover(failed_dc: str, source: str = "us-east-1") -> dict:
    """
    Given a failed data center, find the best alternative destination
    and return Dijkstra results for ALL available targets (ranked by latency).

    If source == failed_dc (the origin node itself went down), we
    automatically promote the lowest-latency online neighbour as the
    new effective source so Dijkstra always has a valid starting point.
    """
    excluded = [failed_dc]

    # Guard: if the selected origin is the failed node, pick the nearest
    # online neighbour of the failed DC as the new source.
    effective_source = source
    if source == failed_dc:
        candidates = [
            (lat, nbr)
            for nbr, lat in LATENCY_GRAPH.get(failed_dc, [])
            if nbr != failed_dc
        ]
        if candidates:
            candidates.sort()
            effective_source = candidates[0][1]
            logger.info(
                "Origin DC %s is the failed node — promoting %s as source",
                source, effective_source,
            )

    results = {}
    for dc_id in DATA_CENTERS:
        if dc_id in (failed_dc, effective_source):
            continue
        results[dc_id] = dijkstra(LATENCY_GRAPH, effective_source, dc_id, excluded)

    ranked = sorted(
        [(dc, r) for dc, r in results.items() if r["reachable"]],
        key=lambda x: x[1]["total_ms"],
    )
    return {
        "failed_dc":      failed_dc,
        "source":         effective_source,
        "original_source": source,
        "alternatives": [
            {
                "dc_id":     dc,
                "dc_name":   DATA_CENTERS[dc]["name"],
                "path":      r["path"],
                "path_names":[DATA_CENTERS[n]["name"] for n in r["path"]],
                "total_ms":  r["total_ms"],
                "hops":      r["hops"],
            }
            for dc, r in ranked
        ],
    }


# ─────────────────────────────────────────────────────────────────────────────
#  IBM WATSONX.AI CLIENT
# ─────────────────────────────────────────────────────────────────────────────
def get_watsonx_model() -> ModelInference:
    api_key    = os.getenv("IBM_API_KEY", "")
    project_id = os.getenv("WATSONX_PROJECT_ID", "")
    url        = os.getenv("WATSONX_URL", "https://us-south.ml.cloud.ibm.com")

    if not api_key or not project_id:
        raise ValueError(
            "IBM_API_KEY and WATSONX_PROJECT_ID must be set in your .env file."
        )

    credentials = Credentials(url=url, api_key=api_key)
    client = APIClient(credentials=credentials, project_id=project_id)

    # Model ID is read from .env so you can change it without touching code.
    # Defaults to llama-3-3-70b-instruct which is available in jp-tok / au-syd.
    model_id = os.getenv("MODEL_ID", "meta-llama/llama-3-3-70b-instruct")

    model = ModelInference(
        model_id=model_id,
        api_client=client,
        params={
            GenParams.MAX_NEW_TOKENS: 1200,
            GenParams.MIN_NEW_TOKENS: 60,
            GenParams.TEMPERATURE:    0.3,
            GenParams.TOP_P:          0.9,
            GenParams.REPETITION_PENALTY: 1.1,
        },
    )
    return model


def call_model(model: ModelInference, system: str, user: str) -> str:
    """
    Call the model using the proper chat messages API.
    Passes system and user as separate roles — no raw header tokens in the text.

    Chat API response shape:
      { "choices": [ { "message": { "content": "..." } } ] }
    """
    messages = [
        {"role": "system", "content": system},
        {"role": "user",   "content": user},
    ]

    # --- preferred: chat() hits the non-deprecated /ml/v1/text/chat endpoint ---
    try:
        raw = model.chat(messages=messages)
        if isinstance(raw, dict):
            choices = raw.get("choices") or []
            if choices:
                content = (choices[0].get("message") or {}).get("content") or ""
                if content.strip():
                    return content.strip()
    except Exception:
        pass  # fall through to generate_text

    # --- fallback: generate_text with a plain concatenated prompt ---
    combined = f"System: {system}\n\nUser: {user}\n\nAssistant:"
    try:
        result = model.generate_text(prompt=combined)
        if isinstance(result, str) and result.strip():
            return result.strip()
    except Exception:
        pass

    # --- last resort: generate() dict path ---
    raw = model.generate(prompt=combined)
    if isinstance(raw, dict):
        results_list = raw.get("results") or []
        if results_list and isinstance(results_list[0], dict):
            text = results_list[0].get("generated_text", "")
            if text:
                return text.strip()

    text = str(raw) if raw is not None else ""
    return text.strip() or "I was unable to generate a response. Please try again."


def build_failover_prompt(
    user_message: str,
    failover_data: dict | None,
    chat_history: list,
) -> tuple[str, str]:
    """
    Return (system_prompt, user_prompt) as separate strings.
    These are passed as distinct chat message roles — no raw Llama tokens.
    """
    history_text = ""
    for turn in chat_history[-6:]:
        role = "Operator" if turn["role"] == "user" else "ARIA"
        history_text += f"{role}: {turn['content']}\n"

    failover_context = ""
    if failover_data:
        alts = failover_data["alternatives"]
        best = alts[0] if alts else None
        dc_info = DATA_CENTERS.get(failover_data["failed_dc"], {})
        failed_name = dc_info.get("name", failover_data["failed_dc"])
        source_id   = failover_data["source"]
        orig_source = failover_data.get("original_source", source_id)

        # Note when origin was auto-promoted
        origin_note = ""
        if orig_source != source_id:
            orig_name = DATA_CENTERS.get(orig_source, {}).get("name", orig_source)
            origin_note = (
                f"\nNOTE: Origin DC {orig_source} ({orig_name}) is the failed node. "
                f"Dijkstra re-routed from nearest online neighbour: "
                f"{source_id} ({DATA_CENTERS[source_id]['name']})."
            )

        all_paths = "\n".join(
            f"  * {a['dc_name']}: {' -> '.join(a['path_names'])} "
            f"({a['total_ms']} ms, {a['hops']} hop{'s' if a['hops'] != 1 else ''})"
            for a in alts
        )
        failover_context = (
            f"\n[DIJKSTRA ROUTING ENGINE OUTPUT]{origin_note}\n"
            f"Failed Node    : {failover_data['failed_dc']} -- {failed_name}\n"
            f"Origin Node    : {source_id} -- {DATA_CENTERS[source_id]['name']}\n"
            f"Timestamp (UTC): {datetime.now(timezone.utc).isoformat(timespec='seconds')}\n"
            f"\nRanked Alternative Routes (lowest latency first):\n{all_paths}\n"
            f"\nRecommended Primary Failover:\n"
            f"  Destination  : {best['dc_name'] if best else 'NONE AVAILABLE'}\n"
            f"  Full Path    : {' -> '.join(best['path_names']) if best else 'N/A'}\n"
            f"  Total Latency: {best['total_ms']} ms\n"
            f"  Hop Count    : {best['hops']}\n"
        )

    system_prompt = f"{AGENT_INSTRUCTIONS.strip()}\n{failover_context}"
    user_prompt   = f"{history_text}Operator: {user_message}"
    return system_prompt, user_prompt


# ─────────────────────────────────────────────────────────────────────────────
#  DC DETECTION HELPER
# ─────────────────────────────────────────────────────────────────────────────
DC_KEYWORDS = {
    "us-east-1":     ["us-east", "us east", "virginia", "us-east-1"],
    "us-west-2":     ["us-west", "us west", "oregon", "us-west-2"],
    "eu-west-1":     ["eu-west", "eu west", "ireland", "eu-west-1", "dublin"],
    "eu-central-1":  ["eu-central", "eu central", "frankfurt", "eu-central-1", "germany"],
    "ap-south-1":    ["ap-south", "ap south", "mumbai", "ap-south-1", "india"],
    "ap-northeast-1":["ap-northeast", "ap northeast", "tokyo", "ap-northeast-1", "japan"],
    "ap-southeast-1":["ap-southeast", "ap southeast", "singapore", "ap-southeast-1"],
    "sa-east-1":     ["sa-east", "sa east", "sao paulo", "são paulo", "sa-east-1", "brazil"],
}

def detect_failed_dc(text: str) -> str | None:
    """Return the first matching DC id found in free-form text, or None."""
    lower = text.lower()
    for dc_id, keywords in DC_KEYWORDS.items():
        for kw in keywords:
            if kw in lower:
                return dc_id
    return None


def is_failover_request(text: str) -> bool:
    """Heuristic: does this message describe an outage / failover request?"""
    triggers = [
        "down", "outage", "offline", "unavailable", "failing", "failed",
        "unreachable", "not responding", "incident", "failover", "reroute",
        "disaster", "p0", "critical", "alert", "degraded",
    ]
    lower = text.lower()
    return any(t in lower for t in triggers)


# ─────────────────────────────────────────────────────────────────────────────
#  FLASK APPLICATION
# ─────────────────────────────────────────────────────────────────────────────
app = Flask(__name__, template_folder="templates")
app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev-secret-key")
CORS(app)

# In-memory incident log (reset on server restart; swap for a DB in production)
incident_log: list[dict] = []


@app.route("/")
def index():
    return render_template("index.html", data_centers=DATA_CENTERS)


@app.route("/api/chat", methods=["POST"])
def chat():
    """Main chat endpoint — parses failover intent, runs Dijkstra, calls Llama."""
    payload      = request.get_json(force=True)
    user_message = payload.get("message", "").strip()
    chat_history = payload.get("history", [])
    source_dc    = payload.get("source_dc", "us-east-1")

    if not user_message:
        return jsonify({"error": "Empty message"}), 400

    failover_data = None
    detected_dc   = None

    if is_failover_request(user_message):
        detected_dc = detect_failed_dc(user_message)
        if detected_dc:
            failover_data = find_best_failover(detected_dc, source=source_dc)
            logger.info(
                "Failover computed: %s → best alt: %s (%d ms)",
                detected_dc,
                failover_data["alternatives"][0]["dc_id"] if failover_data["alternatives"] else "none",
                failover_data["alternatives"][0]["total_ms"] if failover_data["alternatives"] else 0,
            )

    # Build prompt and call Watsonx.ai
    try:
        model             = get_watsonx_model()
        system_p, user_p  = build_failover_prompt(user_message, failover_data, chat_history)
        logger.info("Calling model...")
        ai_reply = call_model(model, system_p, user_p)
    except ValueError as ve:
        logger.error("Configuration error: %s", ve)
        ai_reply = (
            f"⚠️ Configuration Error: {ve}\n\n"
            "Please ensure IBM_API_KEY and WATSONX_PROJECT_ID are set in your .env file."
        )
    except CannotSetProjectOrSpace as e:
        logger.error("Watsonx project not found: %s", e)
        ai_reply = (
            "⚠️ Watsonx Project Not Found\n\n"
            "The WATSONX_PROJECT_ID was not found in the region specified by WATSONX_URL.\n"
            "Ensure WATSONX_URL in your .env matches the region where your project lives.\n\n"
            f"Regions: us-south | jp-tok | eu-gb | eu-de | au-syd\n"
            f"Detail: {e}"
        )
    except WMLClientError as e:
        logger.error("Watsonx model error: %s", e)
        ai_reply = (
            "⚠️ Watsonx Model Error\n\n"
            f"{e}\n\n"
            "To fix: set MODEL_ID in your .env to one of the models available in your project.\n"
            "Available models are listed in the error above."
        )
    except Exception as exc:
        logger.exception("Watsonx.ai call failed")
        ai_reply = f"⚠️ Service Error: {type(exc).__name__} — {exc}"

    # Record incident if applicable
    incident_entry = None
    if failover_data and failover_data["alternatives"]:
        best = failover_data["alternatives"][0]
        incident_entry = {
            "id":          len(incident_log) + 1,
            "timestamp":   datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "severity":    "P0",
            "failed_dc":   failover_data["failed_dc"],
            "failed_name": DATA_CENTERS[failover_data["failed_dc"]]["name"],
            "reroute_to":  best["dc_id"],
            "reroute_name":best["dc_name"],
            "path":        best["path"],
            "path_names":  best["path_names"],
            "latency_ms":  best["total_ms"],
            "hops":        best["hops"],
            "status":      "MITIGATED",
        }
        incident_log.append(incident_entry)

    return jsonify({
        "reply":        ai_reply,
        "failover_data": failover_data,
        "incident":     incident_entry,
        "detected_dc":  detected_dc,
    })


@app.route("/api/graph", methods=["GET"])
def get_graph():
    """Return the full DC graph for frontend visualisation."""
    return jsonify({
        "data_centers": DATA_CENTERS,
        "edges": [
            {"from": src, "to": dst, "latency_ms": lat}
            for src, neighbours in LATENCY_GRAPH.items()
            for dst, lat in neighbours
            if src < dst          # deduplicate bidirectional edges
        ],
    })


@app.route("/api/incidents", methods=["GET"])
def get_incidents():
    return jsonify({"incidents": list(reversed(incident_log))})


@app.route("/api/dijkstra", methods=["POST"])
def run_dijkstra():
    """
    Direct Dijkstra endpoint for manual queries.
    Body: { "source": "us-east-1", "target": "ap-south-1", "excluded": [] }
    """
    body     = request.get_json(force=True)
    source   = body.get("source")
    target   = body.get("target")
    excluded = body.get("excluded", [])

    if not source or not target:
        return jsonify({"error": "source and target are required"}), 400
    if source not in DATA_CENTERS or target not in DATA_CENTERS:
        return jsonify({"error": "Unknown data center ID"}), 400

    result = dijkstra(LATENCY_GRAPH, source, target, excluded)
    result["path_names"] = [DATA_CENTERS[n]["name"] for n in result["path"]]
    return jsonify(result)


@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()})


# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    debug = os.getenv("FLASK_DEBUG", "true").lower() == "true"
    logger.info("Starting Autonomous Cloud Failover Agent on port %d", port)
    app.run(host="0.0.0.0", port=port, debug=debug)
