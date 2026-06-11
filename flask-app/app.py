"""
Flask UI that consumes the DocumentDB MCP server.

Routes:
    GET  /            -> chat UI
    GET  /analysis    -> agentic analysis UI
    GET  /api/status  -> MCP connection + LLM availability
    GET  /api/tools   -> list MCP tools
    POST /api/ask     -> natural-language question -> tool-calling loop -> answer
    POST /api/call    -> manually invoke a tool (name + JSON arguments)
    POST /api/analyze -> multi-step agentic analysis -> markdown report
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from typing import Any, cast

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request

from llm import answer_with_llm, estimate_tokens, get_question_embedding, llm_available
from mcp_client import MCPClient
from qa_cache import QuestionCache
from booking_agent import init_booking

# Add parent directory to path so we can import agent module
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    from agent.travel_agent import run as run_travel_agent
    AGENT_AVAILABLE = True
except ImportError:
    AGENT_AVAILABLE = False

load_dotenv()

# Ensure CONNECTION_PROFILES with write tier is set in the process environment BEFORE MCPClient spawns subprocess
if "CONNECTION_PROFILES" not in os.environ:
    os.environ["CONNECTION_PROFILES"] = '{"default":{"authMode":"connectionString","uriEnv":"DOCUMENTDB_URI","tier":"write"}}'
if "ENABLE_WRITE_TOOLS" not in os.environ:
    os.environ["ENABLE_WRITE_TOOLS"] = "true"
if "ENABLE_MANAGEMENT_TOOLS" not in os.environ:
    os.environ["ENABLE_MANAGEMENT_TOOLS"] = "true"

# How to launch the MCP server. Defaults match the sample's mcp.json.
MCP_COMMAND = os.getenv("MCP_COMMAND", "npx")
MCP_ARGS = os.getenv("MCP_ARGS", "-y github:microsoft/documentdb-mcp").split()
MCP_ENV = {
    "TRANSPORT": os.getenv("TRANSPORT", "stdio"),
    "AUTH_REQUIRED": os.getenv("AUTH_REQUIRED", "false"),
    "TRUST_LOCAL_STDIO": os.getenv("TRUST_LOCAL_STDIO", "true"),
    "CONNECTION_PROFILES": os.getenv("CONNECTION_PROFILES"),
    "DOCUMENTDB_URI": os.getenv(
        "DOCUMENTDB_URI",
        "mongodb://demo:Travel123!@localhost:10260/?tls=true&tlsAllowInvalidCertificates=true",
    ),
    "ENABLE_WRITE_TOOLS": os.getenv("ENABLE_WRITE_TOOLS"),
    "ENABLE_MANAGEMENT_TOOLS": os.getenv("ENABLE_MANAGEMENT_TOOLS"),
}

app = Flask(__name__)
mcp = MCPClient(MCP_COMMAND, MCP_ARGS, MCP_ENV)
init_booking(app, mcp) 
cache_db = os.getenv("CACHE_DB_NAME", "traveldb")
cache_collection = os.getenv("CACHE_COLLECTION_NAME", "qa_history")
cache_similarity = float(os.getenv("CACHE_SIMILARITY_THRESHOLD", "0.97"))
qa_cache = QuestionCache(
    documentdb_uri=MCP_ENV["DOCUMENTDB_URI"],
    db_name=cache_db,
    collection_name=cache_collection,
    similarity_threshold=cache_similarity,
)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/analysis")
def analysis():
    return render_template("analysis.html", agent_available=AGENT_AVAILABLE)


@app.route("/api/status")
def status():
    ok = mcp.connect()
    return jsonify(
        {
            "mcp_connected": ok,
            "mcp_error": None if ok else mcp.last_error,
            "llm_available": llm_available(),
        }
    )


@app.route("/api/tools")
def tools():
    try:
        return jsonify({"tools": mcp.list_tools()})
    except Exception as exc:
        return jsonify({"tools": [], "error": str(exc)}), 502


@app.route("/api/ask", methods=["POST"])
def ask():
    started = time.perf_counter()
    body = request.get_json(silent=True)
    payload = cast(dict[str, Any], body) if isinstance(body, dict) else {}
    question_value = payload.get("question", "")
    question = question_value.strip() if isinstance(question_value, str) else ""
    threshold_value = payload.get("cache_threshold_percent", 97)
    try:
        cache_threshold_percent = int(threshold_value)
    except (TypeError, ValueError):
        cache_threshold_percent = 97
    cache_threshold_percent = max(95, min(100, cache_threshold_percent))
    cache_threshold = cache_threshold_percent / 100.0
    if not question:
        return jsonify({"error": "Empty question"}), 400

    question_vec = get_question_embedding(question)
    try:
        cache_match = qa_cache.find_match(
            question,
            question_vec,
            similarity_threshold=cache_threshold,
        )
    except Exception:
        cache_match = None

    if cache_match:
        doc = cast(dict[str, Any], cache_match["doc"])
        try:
            qa_cache.mark_cache_hit(doc.get("_id"))
        except Exception:
            pass

        elapsed_ms = int((time.perf_counter() - started) * 1000)
        saved = int(doc.get("llm_total_tokens", 0) or estimate_tokens(question + doc.get("answer", "")))
        return jsonify(
            {
                "answer": doc.get("answer", ""),
                "trace": doc.get("trace", []),
                "mode": "cache",
                "cache": {
                    "hit": True,
                    "match_type": cache_match.get("match_type", "exact"),
                    "similarity": float(cache_match.get("similarity", 1.0)),
                    "threshold_percent": cache_threshold_percent,
                },
                "metrics": {
                    "execution_ms": elapsed_ms,
                    "tokens_used": 0,
                    "tokens_saved": saved,
                },
            }
        )

    try:
        tool_list = mcp.list_tools()
    except Exception as exc:
        return jsonify({"error": f"MCP not available: {exc}"}), 502

    def call_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
        return mcp.call_tool(name, args)

    result = answer_with_llm(question, tool_list, call_tool)
    usage = cast(dict[str, int], result.get("usage") or {})
    tokens_used = int(usage.get("total_tokens", 0) or 0)
    if tokens_used <= 0:
        tokens_used = estimate_tokens(question + str(result.get("answer", "")))
        usage["total_tokens"] = tokens_used
    elapsed_ms = int((time.perf_counter() - started) * 1000)

    try:
        qa_cache.store(
            question=question,
            answer=str(result.get("answer", "")),
            trace=cast(list[dict[str, Any]], result.get("trace", [])),
            mode=str(result.get("mode", "llm")),
            question_vector=question_vec,
            llm_usage=usage,
            execution_ms=elapsed_ms,
        )
    except Exception:
        pass

    result["cache"] = {"hit": False, "threshold_percent": cache_threshold_percent}
    result["metrics"] = {
        "execution_ms": elapsed_ms,
        "tokens_used": tokens_used,
        "tokens_saved": 0,
    }
    return jsonify(result)


@app.route("/api/call", methods=["POST"])
def call():
    body = request.get_json(silent=True)
    payload = cast(dict[str, Any], body) if isinstance(body, dict) else {}
    name_value = payload.get("tool")
    args_value = payload.get("arguments")
    name = name_value if isinstance(name_value, str) else ""
    args = cast(dict[str, Any], args_value) if isinstance(args_value, dict) else {}
    if not name:
        return jsonify({"error": "Missing 'tool'"}), 400
    try:
        return jsonify(mcp.call_tool(name, args))
    except Exception as exc:
        return jsonify({"is_error": True, "text": str(exc)}), 502


@app.route("/api/clear-cache", methods=["POST"])
def clear_cache():
    try:
        result = qa_cache.clear_all()
        return jsonify(
            {
                "success": True,
                "deleted_count": result["deleted_count"],
                "message": f"Cleared {result['deleted_count']} cache entries.",
            }
        )
    except Exception as exc:
        return jsonify({"success": False, "error": str(exc)}), 500

@app.route("/api/analyze", methods=["POST"])
def analyze():
    """Run the Travel Operations Analyst agent with a goal and return the markdown report."""
    if not AGENT_AVAILABLE:
        return jsonify({"error": "Agent not available"}), 503

    body = request.get_json(silent=True)
    payload = cast(dict[str, Any], body) if isinstance(body, dict) else {}
    goal = payload.get("goal", "").strip()

    if not goal:
        return jsonify(
            {
                "error": "Empty goal",
                "default_goal": (
                    "Give me a revenue health check: total confirmed revenue, the top 3 "
                    "destination cities by revenue, and any destination that looks "
                    "underbooked. End with 2 recommendations."
                ),
            }
        ), 400

    try:
        # Run the async agent and capture output
        report = asyncio.run(run_travel_agent(goal))
        return jsonify({"report": report, "goal": goal})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="127.0.0.1", port=port, threaded=True, debug=True)
