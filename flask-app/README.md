# Travel Assistant — Flask UI over the DocumentDB MCP server

A small Python **Flask** web app that acts as an **MCP client**. It launches the
DocumentDB MCP server over stdio, lists its tools, and answers natural-language
questions about the `traveldb` data by letting an LLM call those tools. Without an
LLM key it falls back to an **offline preset** mode (keyword-matched demo queries),
so you can see the MCP round-trip working with zero cloud dependencies.

The app also persists Q&A cache entries in DocumentDB (`traveldb.qa_history`) to
avoid repeated LLM tokens for duplicate or semantically similar questions.

```
Browser ──HTTP──> Flask (app.py)
                     │
                     ├── llm.py        NL question ─► tool calls ─► answer
                     └── mcp_client.py stdio ─► github:microsoft/documentdb-mcp ─► DocumentDB
```

## Prerequisites

1. **DocumentDB Local is running and the data is loaded.** This app reuses the
   parent sample. From `documentdb-travel-mcp-sample/`:
   ```bash
   docker compose up -d        # starts DocumentDB Local on :10260
  python ./scripts/load-data.py  # imports traveldb collections + indexes
   ```
2. **Node.js 20+** on your PATH — the app starts the MCP server with
  `npx -y github:microsoft/documentdb-mcp`.
3. **Python 3.10+**.

## Setup

```bash
cd flask-app
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                # then edit if needed
```

Open `.env` and confirm `DOCUMENTDB_URI` matches your DocumentDB credentials
(defaults to `demo:Travel123!` on `localhost:10260`). The sample uses a `default`
connection profile that resolves through `CONNECTION_PROFILES`, and sets
`AUTH_REQUIRED=false` because the app talks to the server over trusted local stdio.

### Optional — enable natural language via an LLM

Out of the box the app runs in **offline preset** mode. To get full
natural-language understanding, add one of the following to `.env`:

**Azure OpenAI** (recommended for Microsoft tenants):
```env
AZURE_OPENAI_API_KEY=...
AZURE_OPENAI_ENDPOINT=https://<your-resource>.openai.azure.com
AZURE_OPENAI_DEPLOYMENT=gpt-4o
AZURE_OPENAI_API_VERSION=2024-10-21
```

**OpenAI:**
```env
OPENAI_API_KEY=sk-...
OPENAI_MODEL=gpt-4o-mini
```

## Run

```bash
python app.py
```

Open **http://127.0.0.1:5000**.

The header shows two badges:
- **MCP: connected / not connected** — whether the MCP server started and reached DocumentDB.
- **LLM: on / offline mode** — whether an LLM key was found.

Type a question or click one of the example prompts. Each answer includes an
expandable **trace** showing the exact MCP tool, arguments, and raw result.

Each answer card also shows runtime metrics:
- cache hit/miss
- tokens used for this ask
- tokens saved if served from cache
- execution time in milliseconds

## Cache collection

Collection: `traveldb.qa_history` (configurable)

Each entry stores:
- question + normalized question
- answer + MCP trace
- question embedding vector (when embedding model is configured)
- token usage metadata
- execution time + cache hit counters

Matching strategy:
1. exact match on normalized question
2. fallback vector similarity search (cosine) over recent entries

Environment variables in `.env`:
- `CACHE_DB_NAME` (default `traveldb`)
- `CACHE_COLLECTION_NAME` (default `qa_history`)
- `CACHE_SIMILARITY_THRESHOLD` (default `0.97`)

Optional embedding config:
- Azure OpenAI: `AZURE_OPENAI_EMBEDDING_DEPLOYMENT`
- OpenAI: `OPENAI_EMBEDDING_MODEL`

## Try these

- How many confirmed reservations, and total booked revenue?
- Top 3 destination cities by total revenue.
- Average nights stayed per region.
- Who are my top 5 customers by spend?
- Revenue by month for 2026.
- Show 5 sample reservations.

> In offline mode the keyword presets cover the prompts above. Add an LLM key to
> ask anything in free-form English.

## Routes

| Method | Path          | Purpose                                            |
|--------|---------------|----------------------------------------------------|
| GET    | `/`           | Chat UI                                             |
| GET    | `/api/status` | MCP connection state + LLM availability            |
| GET    | `/api/tools`  | List the MCP server's tools                         |
| POST   | `/api/ask`    | `{question}` → tool-calling loop → `{answer,trace}` |
| POST   | `/api/call`   | `{tool, arguments}` → invoke one tool directly      |

## Troubleshooting

- **MCP: not connected** — make sure `docker compose up -d` is running and
  `python ./scripts/load-data.py` succeeded. Check `DOCUMENTDB_URI` in `.env`. The error
  text is shown in the UI and at `/api/status`.
- **`npm error code ENOVERSIONS` for `documentdb-mcp-server`** — use
  `github:microsoft/documentdb-mcp` in `MCP_ARGS`; this sample is already configured
  that way.
- **`AUTH_REQUIRED=true requires ENTRA_*`** — your shell launched the server without the
  app's stdio env vars. Use the Flask app's `.env`, or set `TRANSPORT=stdio`,
  `AUTH_REQUIRED=false`, `TRUST_LOCAL_STDIO=true`, and `CONNECTION_PROFILES=...`
  before invoking `npx` manually.
- **`npx` not found** — install Node.js 20+, or set `MCP_COMMAND` / `MCP_ARGS` in
  `.env` to point at your own server launch command.
- **LLM: offline mode** but you added a key — confirm the variable names match the
  block above and that you restarted `python app.py`.
- **TLS / certificate errors** — the local container uses a self-signed cert; the
  sample URI includes `tls=true&tlsAllowInvalidCertificates=true` for that reason.
  Do not use that flag against a real Azure Cosmos DB for MongoDB (vCore) endpoint.
