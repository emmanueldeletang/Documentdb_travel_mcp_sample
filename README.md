# DocumentDB + MCP — Travel & Reservations Sample

A ready-to-run sample that shows how to query a travel/reservations database in **plain
English** through the [Azure DocumentDB Agent Kit](https://github.com/Azure/documentdb-agent-kit).

You spin up DocumentDB locally with Docker, load sample travel data, point the kit's
**DocumentDB MCP server** at it, then ask your agent (Claude Code, Cursor, Copilot, Gemini
CLI, …) questions like *"What's my total booked revenue by destination?"* — the agent
translates that into a MongoDB query, runs it through MCP, and answers.

```
You (natural language)
      │
      ▼
Agent + documentdb-agent-kit skills   ──►  DocumentDB MCP server (find/aggregate/count …)
      │                                              │
      ▼                                              ▼
"Top 3 cities by revenue"                   DocumentDB Local (Docker, port 10260)
```

---

## What's in this folder

| Path | Purpose |
|------|---------|
| `docker-compose.yml` | Runs **DocumentDB Local** (MongoDB-compatible) on port 10260 |
| `.env.example` | Credentials + connection string — copy to `.env` |
| `mcp.json` | Wires the **DocumentDB MCP server** to your local DB |
| `data/*.json` | Sample data: destinations, flights, customers, reservations |
| `scripts/load-data.py` | Imports the data and creates indexes |
| `flask-app/` | Optional web app with MCP tool-calling + DocumentDB Q&A cache |
| `NL-QUERIES.md` | **The fun part** — natural-language prompts → MCP tool → underlying MongoDB query |

**Database:** `traveldb` — collections: `destinations` (10), `flights` (10),
`customers` (12), `reservations` (24).

When using `flask-app`, an extra collection is created:
- `qa_history` — stores question/answer history, question embedding vectors, token usage, and response timing.

---

## Flask App Architecture (Cache + Vector Search)

The Flask app now adds a persistence layer to reduce repeated LLM cost:

1. User asks a question.
2. App computes a question embedding (when embedding model is configured).
3. App checks `traveldb.qa_history`:
  - exact normalized-question match first
  - then vector similarity match (`CACHE_SIMILARITY_THRESHOLD`, default `0.97`)
4. If cache hit: return cached answer and trace immediately.
5. If cache miss: run MCP tool-calling + LLM answer, then save to `qa_history`.

Each `/api/ask` response includes:
- cache hit/miss
- token used by this ask
- token saved when served from cache
- execution time (ms)

This makes repeat and near-repeat questions faster and cheaper while keeping
full traceability.

---

## Prerequisites

- **Docker** (Desktop or Engine)
- **Node.js 20+** (the MCP server runs via `npx`)
- **Python 3.10+** with **pymongo**
  - `pip install "pymongo[srv]"`
- An **Agent Skills–compatible client** with the DocumentDB Agent Kit installed:
  ```bash
  npx skills add Azure/documentdb-agent-kit     # say "yes" to the find-skills helper
  ```

---

## Setup (5 steps)

### 1. Configure credentials
```bash
cp .env.example .env
# (optional) edit the username/password in .env
```

### 2. Start DocumentDB Local
```bash
docker compose up -d
docker ps            # confirm docdb-travel is "Up" on 0.0.0.0:10260->10260
```

### 3. Load the sample data
```bash
python ./scripts/load-data.py
```
Expected tail:
```
Done. Document counts:
  destinations: 10
  flights: 10
  customers: 12
  reservations: 24
```

### 4. Register the MCP server with your agent
Copy `mcp.json` into your client's MCP config location (or merge the `documentdb` entry):

| Client | Where |
|--------|-------|
| **Claude Code** | project root `.mcp.json`, or `claude mcp add` |
| **Cursor** | `.cursor/mcp.json` |
| **VS Code / Copilot** | `.vscode/mcp.json` |
| **Gemini CLI** | `~/.gemini/settings.json` |

The server is launched with `npx -y github:microsoft/documentdb-mcp`.
This server is not currently published to npm, so the old `documentdb-mcp-server`
package name no longer works. For local stdio use, configure a trusted local transport
plus an administrator-defined connection profile:
```json
"TRANSPORT": "stdio",
"AUTH_REQUIRED": "false",
"TRUST_LOCAL_STDIO": "true",
"CONNECTION_PROFILES": "{\"default\":{\"authMode\":\"connectionString\",\"uriEnv\":\"DOCUMENTDB_URI\"}}",
"DOCUMENTDB_URI": "mongodb://your:your!@mongocluster.cosmos.azure.com/?tls=true&tlsAllowInvalidCertificates=true",
"ENABLE_WRITE_TOOLS": "true",
"ENABLE_MANAGEMENT_TOOLS": "true"
```
If you want to launch it directly in PowerShell for debugging, set the env vars in the
same shell before running `npx`:
```powershell
$env:TRANSPORT = "stdio"
$env:AUTH_REQUIRED = "false"
$env:TRUST_LOCAL_STDIO = "true"
$env:CONNECTION_PROFILES = '{"default":{"authMode":"connectionString","uriEnv":"DOCUMENTDB_URI"}}'
$env:DOCUMENTDB_URI = 'mongodb://your:your!@mongocluster.cosmos.azure.com/?tls=true&tlsAllowInvalidCertificates=true'
npx -y github:microsoft/documentdb-mcp
```
> Leave these out (or set `false`) if you want a strictly **read-only** agent.

### 5. Restart the agent and verify
Ask your agent:
> *"Use the documentdb tools with connection_profile 'default' — list the databases, then show 3 sample reservations from traveldb."*

It should call `list_databases` and then `sample_documents` or `find_documents`, and return rows from `traveldb`.

---

## Now ask it things in natural language

Open `NL-QUERIES.md` for a full catalog. A few to start with:

- *"How many confirmed reservations do we have, and what's the total booked revenue?"*
- *"Top 3 destination cities by total revenue."*
- *"Which customers are platinum or gold tier and have a trip to Tokyo?"*
- *"Average nights stayed per region."*
- *"This query is slow — how should I index reservations filtered by status and check-in date?"*

The agent picks the right DocumentDB MCP tool (`count_documents`, `aggregate`,
`find_documents`, `optimize_find_query`, …), and the kit's skills guide it toward correct
MongoDB query/aggregation syntax and good indexing.

---

## Using Azure DocumentDB instead of local

Swap one value. In `.env` / `mcp.json`, set `DOCUMENTDB_URI` to your cluster's connection
string from the Azure portal (**Settings → Connection strings**):
```
mongodb+srv://<user>:<password>@<cluster>.mongocluster.cosmos.azure.com/?tls=true&authMechanism=SCRAM-SHA-256
```
Then run `python ./scripts/load-data.py` and restart the agent. The connection profile can
stay named `default`; only the URI changes. For provisioning a cluster, the kit ships an
`azure-deployment` skill (Bicep / az CLI / Terraform).

---

## Teardown
```bash
docker compose down        # stop, keep data volume
docker compose down -v     # stop and delete data
```

---

## Notes & troubleshooting

- **TLS:** DocumentDB Local uses a self-signed cert, so the connection string includes
  `tls=true&tlsAllowInvalidCertificates=true`. That's expected for local dev only — a real
  Azure cluster uses a valid cert (drop `tlsAllowInvalidCertificates`).
- **Loader can't connect:** give the container a few more seconds on first run (the image
  initializes Postgres + gateway), then re-run `python ./scripts/load-data.py`.
- **Agent doesn't see the tools:** fully **quit and reopen** the client after editing
  `mcp.json` — a reload isn't always enough to pick up new env vars.
- **`npm error code ENOVERSIONS` for `documentdb-mcp-server`:** use
  `npx -y github:microsoft/documentdb-mcp` instead. The server currently runs from the
  GitHub repo rather than a published npm package.
- **`AUTH_REQUIRED=true requires ENTRA_*` errors:** you launched the server without the
  local stdio env vars. Set `TRANSPORT=stdio`, `AUTH_REQUIRED=false`,
  `TRUST_LOCAL_STDIO=true`, and `CONNECTION_PROFILES=...` in the same shell session.
- **Missing `pymongo`:** run `pip install "pymongo[srv]"` and retry.
- **Flask cache not persisting:** ensure `flask-app/requirements.txt` is installed
  (includes `pymongo[srv]`) and that `DOCUMENTDB_URI` points to a writable cluster.
- **No vector cache hits:** set an embedding model in Flask env
  (`AZURE_OPENAI_EMBEDDING_DEPLOYMENT` or `OPENAI_EMBEDDING_MODEL`) and tune
  `CACHE_SIMILARITY_THRESHOLD`.

> The data here is **synthetic** (seeded random) and meant only for demos.
