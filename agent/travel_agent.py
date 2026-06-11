"""
Travel Operations Analyst — a standalone agentic CLI over the DocumentDB MCP server.

Unlike the Flask app (single-turn Q&A), this agent runs a multi-step loop:
it plans, calls DocumentDB MCP tools across several iterations, observes each
result, and only then writes a final report.

Usage:
    python agent/travel_agent.py "Give me a revenue health check across all destinations"
    python agent/travel_agent.py            # falls back to a built-in demo goal
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ---- config (mirrors flask-app/.env conventions) ----------------------------
MCP_COMMAND = os.environ.get("MCP_COMMAND", "npx")
MCP_ARGS = os.environ.get("MCP_ARGS", "-y github:microsoft/documentdb-mcp").split()
CONNECTION_PROFILE = os.environ.get("CONNECTION_PROFILE", "default")
MAX_STEPS = int(os.environ.get("AGENT_MAX_STEPS", "8"))

# Same trusted-local-stdio env the README documents for the MCP server.
MCP_ENV = {
    "TRANSPORT": "stdio",
    "AUTH_REQUIRED": "false",
    "TRUST_LOCAL_STDIO": "true",
    "CONNECTION_PROFILES": json.dumps(
        {"default": {"authMode": "connectionString", "uriEnv": "DOCUMENTDB_URI"}}
    ),
    "DOCUMENTDB_URI": os.environ.get(
        "DOCUMENTDB_URI",
        "mongodb://demo:Travel123!@localhost:10260/"
        "?tls=true&tlsAllowInvalidCertificates=true",
    ),
}

SYSTEM_PROMPT = f"""You are a Travel Operations Analyst for the `traveldb` database
(collections: destinations, flights, customers, reservations).

You work by calling DocumentDB MCP tools. Rules:
- ALWAYS pass connection_profile="{CONNECTION_PROFILE}" to every tool call.
- Plan before you query. Break the goal into concrete steps and run one tool at a time.
- Prefer aggregate pipelines for revenue/grouping; use count_documents for totals.
- Never invent numbers — every figure in your report must come from a tool result.
- When you have enough evidence, STOP calling tools and write a concise markdown
  report with: Summary, Key Findings (with the numbers), and Recommendations.
"""


# ---- LLM client (Azure OpenAI or OpenAI) ------------------------------------
def make_llm():
    if os.environ.get("AZURE_OPENAI_API_KEY"):
        from openai import AzureOpenAI
        client = AzureOpenAI(
            api_key=os.environ["AZURE_OPENAI_API_KEY"],
            azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
            api_version=os.environ.get("AZURE_OPENAI_API_VERSION", "2024-10-21"),
        )
        return client, os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-4o")
    if os.environ.get("OPENAI_API_KEY"):
        from openai import OpenAI
        return OpenAI(), os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
    sys.exit("No LLM key found. Set AZURE_OPENAI_API_KEY or OPENAI_API_KEY in .env.")


def to_openai_tools(mcp_tools) -> list[dict]:
    """Convert MCP tool definitions to OpenAI function-tool schema."""
    out = []
    for t in mcp_tools:
        params = t.inputSchema or {"type": "object", "properties": {}}
        out.append({
            "type": "function",
            "function": {
                "name": t.name,
                "description": (t.description or "").strip()[:1024],
                "parameters": params,
            },
        })
    return out


def flatten(result) -> str:
    """Flatten MCP content blocks into a single text payload (as mcp_client.py does)."""
    parts = []
    for block in getattr(result, "content", []) or []:
        text = getattr(block, "text", None)
        parts.append(text if text is not None else str(block))
    return "\n".join(parts) or "(empty result)"


# ---- the agent loop ---------------------------------------------------------
async def run(goal: str) -> str:
    """Run the agent and return the final report as a string."""
    llm, model = make_llm()
    params = StdioServerParameters(command=MCP_COMMAND, args=MCP_ARGS,
                                   env={**os.environ, **MCP_ENV})

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tool_list = (await session.list_tools()).tools
            tools = to_openai_tools(tool_list)
            print(f"[connected] {len(tools)} DocumentDB tools available\n")

            messages = [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": goal},
            ]

            for step in range(1, MAX_STEPS + 1):
                resp = llm.chat.completions.create(
                    model=model, messages=messages,
                    tools=tools, tool_choice="auto", temperature=0,
                )
                msg = resp.choices[0].message
                messages.append(msg.model_dump(exclude_none=True))

                if not msg.tool_calls:
                    report = msg.content
                    print("=" * 70)
                    print(report)
                    return report

                for call in msg.tool_calls:
                    args = json.loads(call.function.arguments or "{}")
                    args.setdefault("connection_profile", CONNECTION_PROFILE)
                    print(f"[step {step}] → {call.function.name}({json.dumps(args)})")
                    try:
                        result = await session.call_tool(call.function.name, args)
                        payload = flatten(result)
                    except Exception as exc:  # surface tool errors back to the model
                        payload = f"ERROR: {type(exc).__name__}: {exc}"
                    print(f"          ← {payload[:200]}\n")
                    messages.append({
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": payload[:6000],
                    })

            final_msg = "[stopped] reached MAX_STEPS without a final answer."
            print(final_msg)
            return final_msg


if __name__ == "__main__":
    goal = " ".join(sys.argv[1:]) or (
        "Give me a revenue health check: total confirmed revenue, the top 3 "
        "destination cities by revenue, and any destination that looks "
        "underbooked. End with 2 recommendations."
    )
    asyncio.run(run(goal))
