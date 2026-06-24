# Docket Intelligence

Monitors court dockets and cross-references them against a client database to surface
business development opportunities and conflict flags for law firms.

Built with MCP (Model Context Protocol), Claude, and Python. Runs locally with SQLite;
deploys to Azure with SharePoint as the data layer.

---

## Architecture

```
Scheduler (cron / Azure Logic App)
        │
        ▼
Agent Orchestrator  ←──── Claude API (entity extraction + classification)
        │
        ├──► Docket Monitor MCP Server   (CourtListener / Docket Alarm)
        ├──► Client Intel MCP Server     (SQLite locally / SharePoint in prod)
        └──► Notifications MCP Server    (log file locally / Graph API in prod)
```

---

## Local Setup

### 1. Clone and install dependencies

```bash
git clone <repo>
cd docket-intelligence
python -m venv .venv
source .venv/bin/activate       # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
# Edit .env and add your ANTHROPIC_API_KEY at minimum
```

### 3. Seed the local client database

```bash
python scripts/seed_clients.py
```

### 4. Run the agent

```bash
# Dry run — fetches and analyzes dockets but writes nothing
python -m src.agent.orchestrator --dry-run

# Live run — logs opportunities to SQLite, sends mock notifications
python -m src.agent.orchestrator

# Filter by court and date
python -m src.agent.orchestrator --court nysd --date-from 2024-01-01
```

### 5. Run tests

```bash
pytest tests/ -v
```

---

## Project Structure

```
docket-intelligence/
├── src/
│   ├── models/
│   │   └── models.py              # Pydantic data models (Docket, Client, Opportunity, …)
│   ├── mcp_servers/
│   │   ├── docket_monitor/
│   │   │   └── server.py          # MCP server: fetches dockets from CourtListener
│   │   ├── client_intel/
│   │   │   ├── server.py          # MCP server: client DB operations
│   │   │   └── sqlite_repo.py     # SQLite adapter (swap for Graph adapter in prod)
│   │   └── notifications/
│   │       └── server.py          # MCP server: Teams/email/tasks (logs locally)
│   └── agent/
│       └── orchestrator.py        # Core AI loop connecting all three servers
├── scripts/
│   └── seed_clients.py            # Populate local DB with test clients
├── tests/
│   └── test_client_repo.py        # Unit tests for SQLite repo and matching
├── data/                          # Local SQLite DB and notification logs (git-ignored)
├── .env.example
└── requirements.txt
```

---

## Swapping to Production (Microsoft)

The local → production swap is controlled by one env variable: `ENV=production`.

When `ENV=production`, the Client Intel server loads `graph_adapter.py` instead of
`sqlite_repo.py`. The MCP tool interface is identical — only the data layer changes.

See `DEPLOYMENT.md` for Azure setup instructions.

---

## CourtListener Wrapper Server

`src/mcp_servers/courtlistener_wrapper/server.py` is a unified server that combines
the official CourtListener hosted MCP server with this project's conflict-checking and
opportunity-management tools. Use it when you want a single connection point instead of
running three separate servers.

### What it exposes

| Source | Tools |
|--------|-------|
| Official CL MCP (proxied) | All tools from `mcp.courtlistener.com` — search, opinions, citations, alerts, judge data, etc. Auto-updates as CourtListener adds new tools. |
| Conflict & client intel | `check_conflicts`, `find_entity_matches`, `log_opportunity`, `list_opportunities`, `update_opportunity_status` |
| Combined | `search_filings_with_conflicts` — fetch dockets + run conflict check in one call; `check_party_in_courts` — find all cases for a named entity + check if they're a client |

### Connecting

If `COURTLISTENER_API_TOKEN` is set, the wrapper connects to the official CourtListener
MCP server via OAuth SSE and proxies its full tool set. Without a token it runs in
local-only mode (direct REST API + conflict tools only).

```bash
# Run the wrapper standalone (e.g. to wire into Claude Desktop or another MCP host)
python -m src.mcp_servers.courtlistener_wrapper.server
```

To point the orchestrator at the wrapper instead of the three individual servers,
replace the `StdioServerParameters` in `orchestrator.py` with a single entry:

```python
WRAPPER_SERVER = StdioServerParameters(
    command="python",
    args=["-m", "src.mcp_servers.courtlistener_wrapper.server"],
)
```

---

## Adding a New Docket Data Source

1. Create `src/mcp_servers/docket_monitor/adapters/your_source.py`
2. Implement `fetch_dockets(...)` returning `list[Docket]`
3. Set `DOCKET_SOURCE=your_source` in `.env`
4. The server picks up the new adapter via the factory in `server.py`

---

## Key Design Decisions

- **Adapter pattern** — every external dependency sits behind an interface, making the
  local↔production swap clean and testable without cloud access.
- **MCP over direct function calls** — each server can be tested, replaced, or scaled
  independently. The agent only knows tool names and schemas, not implementations.
- **Human in the loop** — the agent surfaces and classifies; attorneys decide. No
  automated outreach without human approval.
- **Tenant-local in production** — client data never leaves the Microsoft 365 tenant.
  The only external calls are reads from court data APIs and the Claude API.
