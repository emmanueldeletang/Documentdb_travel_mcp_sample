"""
Natural-language -> MCP tool-call layer.

Two modes:
  * LLM mode  - if Azure OpenAI or OpenAI credentials are set, the model is given
                the MCP tools as function definitions and drives a tool-calling
                loop against the DocumentDB MCP server.
  * Offline mode - if no LLM is configured, a small keyword matcher maps a handful
                of common questions to deterministic tool calls so the demo still
                runs. Configure an LLM for real free-form natural language.
"""
from __future__ import annotations

import json
import os
from typing import Any, Callable, cast

SYSTEM_PROMPT = """You are a data analyst for a travel agency. You answer questions
about a MongoDB-compatible database (Azure DocumentDB) named 'traveldb' with collections:
  - reservations(_id, customer_id, customer_name, destination_id, destination_city,
      flight_id, status[confirmed|completed|cancelled|pending], booking_date, check_in,
      check_out, nights, travelers, room_total, flight_total, total_price, currency,
      payment{method,paid})  -- dates are 'YYYY-MM-DD' strings
  - destinations(_id, city, country, region, category, avg_nightly_rate, rating, tags[])
  - customers(_id, name, email, country, loyalty_tier, loyalty_points, joined)
  - flights(_id, airline, origin, destination, dest_city, duration_min, cabin, base_fare)

Use the provided DocumentDB tools to answer. Prefer 'aggregate' for analytics,
'find_documents' for lookups, 'count_documents' for counts. Always pass
connection_profile 'default', db_name 'traveldb', and collection_name when the tool
needs one. Use 'query' for filters and 'options' for limit/sort/projection values.
"Booked revenue" means status in [confirmed, completed]. After getting tool results,
answer the user concisely in plain English and include the key numbers."""


# --------------------------------------------------------------------------- #
# LLM client selection
# --------------------------------------------------------------------------- #
def _make_client():
    """Return (client, model, kind) or (None, None, None) if not configured."""
    az_key = os.getenv("AZURE_OPENAI_API_KEY")
    az_endpoint = os.getenv("AZURE_OPENAI_ENDPOINT")
    az_deploy = os.getenv("AZURE_OPENAI_DEPLOYMENT")
    if az_key and az_endpoint and az_deploy:
        from openai import AzureOpenAI

        client = AzureOpenAI(
            api_key=az_key,
            azure_endpoint=az_endpoint,
            api_version=os.getenv("AZURE_OPENAI_API_VERSION", "2024-10-21"),
        )
        return client, az_deploy, "azure"

    oa_key = os.getenv("OPENAI_API_KEY")
    if oa_key:
        from openai import OpenAI

        client = OpenAI(api_key=oa_key)
        return client, os.getenv("OPENAI_MODEL", "gpt-4o-mini"), "openai"

    return None, None, None


def llm_available() -> bool:
    client, _, _ = _make_client()
    return client is not None


def estimate_tokens(text: str) -> int:
    # Rough estimate used only when provider token usage is unavailable.
    return max(1, len(text) // 4)


def get_question_embedding(question: str) -> list[float] | None:
    client, _, kind = _make_client()
    if client is None:
        return None

    try:
        if kind == "azure":
            embed_model = os.getenv("AZURE_OPENAI_EMBEDDING_DEPLOYMENT", "").strip()
            if not embed_model:
                return None
            emb = client.embeddings.create(model=embed_model, input=question)
        else:
            embed_model = os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")
            emb = client.embeddings.create(model=embed_model, input=question)
        return list(emb.data[0].embedding)
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# Tool schema conversion (MCP -> OpenAI function tools)
# --------------------------------------------------------------------------- #
def _to_openai_tools(mcp_tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for t in mcp_tools:
        out.append(
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t["description"][:1024],
                    "parameters": t["input_schema"],
                },
            }
        )
    return out


# --------------------------------------------------------------------------- #
# LLM-driven tool-calling loop
# --------------------------------------------------------------------------- #
def answer_with_llm(
    question: str,
    mcp_tools: list[dict[str, Any]],
    call_tool: Callable[[str, dict[str, Any]], dict[str, Any]],
    max_steps: int = 5,
) -> dict[str, Any]:
    client, model, _ = _make_client()
    if client is None:
        return offline_answer(question, call_tool)
    if model is None:
        return offline_answer(question, call_tool)

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": question},
    ]
    tools = _to_openai_tools(mcp_tools)
    trace: list[dict[str, Any]] = []
    usage_totals = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

    for _ in range(max_steps):
        resp = client.chat.completions.create(
            model=model,
            messages=cast(Any, messages),
            tools=cast(Any, tools),
            temperature=0,
        )
        usage = getattr(resp, "usage", None)
        if usage is not None:
            usage_totals["prompt_tokens"] += int(getattr(usage, "prompt_tokens", 0) or 0)
            usage_totals["completion_tokens"] += int(getattr(usage, "completion_tokens", 0) or 0)
            usage_totals["total_tokens"] += int(getattr(usage, "total_tokens", 0) or 0)

        msg = resp.choices[0].message
        if not msg.tool_calls:
            if usage_totals["total_tokens"] <= 0:
                usage_totals["total_tokens"] = estimate_tokens(question + (msg.content or ""))
            return {
                "answer": msg.content or "",
                "trace": trace,
                "mode": "llm",
                "usage": usage_totals,
            }

        messages.append(
            {
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                    }
                    for tc in msg.tool_calls
                ],
            }
        )
        for tc in msg.tool_calls:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            if not isinstance(args, dict):
                args = {}
            typed_args = cast(dict[str, Any], args)
            result = call_tool(tc.function.name, typed_args)
            trace.append({"tool": tc.function.name, "arguments": args, "result": result["text"]})
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result["text"][:8000] or "(no output)",
                }
            )

    if usage_totals["total_tokens"] <= 0:
        usage_totals["total_tokens"] = estimate_tokens(question)
    return {
        "answer": "Stopped after several steps without a final answer.",
        "trace": trace,
        "mode": "llm",
        "usage": usage_totals,
    }


# --------------------------------------------------------------------------- #
# Offline keyword fallback (no LLM key required)
# --------------------------------------------------------------------------- #
DB = "traveldb"
PROFILE = "default"

_PRESETS: list[tuple[list[str], dict[str, Any]]] = [
    (
        ["revenue", "by", "destination"],
        {
            "tool": "aggregate",
            "arguments": {
                "connection_profile": PROFILE,
                "db_name": DB,
                "collection_name": "reservations",
                "pipeline": [
                    {"$match": {"status": {"$in": ["confirmed", "completed"]}}},
                    {"$group": {"_id": "$destination_city", "revenue": {"$sum": "$total_price"}}},
                    {"$sort": {"revenue": -1}},
                ],
            },
        },
    ),
    (
        ["total", "revenue"],
        {
            "tool": "aggregate",
            "arguments": {
                "connection_profile": PROFILE,
                "db_name": DB,
                "collection_name": "reservations",
                "pipeline": [
                    {"$match": {"status": {"$in": ["confirmed", "completed"]}}},
                    {"$group": {"_id": None, "revenue": {"$sum": "$total_price"}, "trips": {"$sum": 1}}},
                ],
            },
        },
    ),
    (
        ["top", "customers"],
        {
            "tool": "aggregate",
            "arguments": {
                "connection_profile": PROFILE,
                "db_name": DB,
                "collection_name": "reservations",
                "pipeline": [
                    {"$match": {"status": {"$in": ["confirmed", "completed"]}}},
                    {"$group": {"_id": "$customer_id", "name": {"$first": "$customer_name"},
                                "spend": {"$sum": "$total_price"}}},
                    {"$sort": {"spend": -1}},
                    {"$limit": 5},
                ],
            },
        },
    ),
    (
        ["confirmed", "count"],
        {
            "tool": "count_documents",
            "arguments": {
                "connection_profile": PROFILE,
                "db_name": DB,
                "collection_name": "reservations",
                "query": {"status": "confirmed"},
            },
        },
    ),
    (
        ["sample", "reservations"],
        {
            "tool": "find_documents",
            "arguments": {
                "connection_profile": PROFILE,
                "db_name": DB,
                "collection_name": "reservations",
                "query": {},
                "options": {"limit": 5},
            },
        },
    ),
    (
        ["list", "databases"],
        {"tool": "list_databases", "arguments": {"connection_profile": PROFILE}},
    ),
]


def offline_answer(
    question: str, call_tool: Callable[[str, dict[str, Any]], dict[str, Any]]
) -> dict[str, Any]:
    q = question.lower()
    chosen = None
    best = 0
    for keywords, spec in _PRESETS:
        score = sum(1 for k in keywords if k in q)
        if score > best:
            best, chosen = score, spec
    if not chosen or best == 0:
        return {
            "answer": (
                "Offline demo mode: I couldn't map that question to a preset. "
                "Configure an LLM (see README) for free-form natural language, or try: "
                "'total revenue by destination', 'top customers', 'count confirmed', "
                "'sample reservations', 'list databases'."
            ),
            "trace": [],
            "mode": "offline",
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }
    result = call_tool(chosen["tool"], chosen["arguments"])
    return {
        "answer": f"Result from `{chosen['tool']}` (offline preset):",
        "trace": [{"tool": chosen["tool"], "arguments": chosen["arguments"], "result": result["text"]}],
        "mode": "offline",
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }
