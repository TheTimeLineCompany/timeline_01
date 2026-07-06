# Timeline

Timeline is a local-first Wikipedia exploration system. It renders an article immediately, builds a graph of section-level links, enriches sections and connected articles in the background, and projects dated context into a continuous article-wide timeline.

The read path is intentionally cache-first: article text, links, graph framework, deterministic dates, CPU entities, embeddings, and cached scores can work without calling an LLM. Optional LLM lanes run behind the reader for deeper temporal extraction, relevance adjudication, and agent insights.

## Repository Layout

- `backend/` - FastAPI service, Postgres cache schema, Neo4j graph adapter, orchestration workers, CPU extraction, embeddings, and optional LLM lanes.
- `frontend/` - React/Vite reader, timeline, graph view, status panels, and settings UI.
- `scripts/` - Portable local startup, stop, health-check, worker, and vLLM watchdog scripts.
- `specs/` - Product, architecture, scoring, graph, and enrichment specifications. This folder is deliberately kept separate from implementation code.
- `start-timeline.bat` / `stop-timeline.bat` - Windows convenience launchers.

Internal implementation tables currently use the `timeline_v4` Postgres schema name. That is a cache namespace, not the public project name.

## Prerequisites

Timeline expects these services to already exist. The repository does not ship credentials, data dumps, model weights, Python virtual environments, or private machine paths.

- Python 3.12.
- Node.js LTS with npm.
- PostgreSQL 16+ with `pg_trgm` and `pgvector` enabled.
- A local/wiki article database prepared by the companion **Local Wiki Data Setup** project.
- Neo4j with Bolt enabled.
- Optional OpenAI-compatible vLLM endpoint for LLM enrichment.

## Local Wiki Data Setup

The raw wiki content is intentionally managed as a separate project: **Local Wiki Data Setup**. That tool should import and prepare the source wiki tables that Timeline reads from.

Expected source-table contract:

- `public."wiki_content_CSV_V4"`
- `public."wiki_content_lookup_V4"`

Timeline treats those tables as read-only truth. Application caches, embeddings, extracted entities, temporal rows, related-cache rows, job state, and scoring artifacts live under the configured Postgres cache schema, defaulting to `timeline_v4`.

Recommended companion repository name:

- `TheTimeLineCompany/local-wiki-data-setup`

## Configuration

Copy the backend example environment file and fill in local values:

```powershell
cd backend
Copy-Item .env.example .env
```

Important settings:

- `PG_HOST`, `PG_PORT`, `PG_USER`, `PG_PASSWORD`, `PG_DATABASE`, `PG_SCHEMA`
- `NEO4J_URI`, `NEO4J_USER`, `NEO4J_PASSWORD`, `NEO4J_DATABASE`
- `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`
- `TIMELINE_V4_LLM_LANE_ENABLED`

Do not commit `.env`. The repository ignores it.

## vLLM / Model Serving

Timeline connects to a model server through the OpenAI-compatible API shape exposed by vLLM. By default the app checks:

```text
http://127.0.0.1:8101/v1/models
```

Set `LLM_BASE_URL` if your endpoint is elsewhere. Set `LLM_API_KEY` only if your serving stack requires one.

This repository does not prescribe how to serve the model. Any vLLM setup is acceptable if it provides:

- `/v1/models`
- `/v1/chat/completions`
- JSON-compatible responses for guided extraction prompts

The app can run in core mode without vLLM:

```powershell
$env:TIMELINE_V4_CORE_MODE = "1"
.\start-timeline.bat
```

Core mode loads articles, parses L0/L1/L2 links, builds the graph framework, runs CPU dates/entities/embeddings where configured, and avoids GPU/LLM calls.

## Neo4j

Neo4j stores the neutral graph backbone. Nodes do not store `L1` or `L2`; those levels are computed at query time from the currently focused article/section.

Canonical relationship vocabulary:

- `HAS_SECTION`
- `LINKS_TO`
- `RELATED_TO`

Use environment settings to point the backend to your Neo4j database.

## Install

Backend:

```powershell
cd backend
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\pip.exe install -e .[dev,embeddings]
```

Optional CPU entity packages:

```powershell
.\.venv\Scripts\pip.exe install -e .[gliner2]
```

Frontend:

```powershell
cd frontend
npm install
```

Initialize or update the application schema after the database settings are configured:

```powershell
cd backend
.\.venv\Scripts\python.exe scripts\init_schema.py
.\.venv\Scripts\python.exe scripts\graph_schema_check.py --apply
```

## Run

Start the app:

```powershell
.\start-timeline.bat
```

Stop only Timeline app processes, leaving external model serving alone:

```powershell
.\stop-timeline.bat
```

Manual services:

```powershell
cd backend
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000

cd frontend
npm run dev -- --host 127.0.0.1 --port 5174
```

## Development Checks

Backend tests:

```powershell
cd backend
.\.venv\Scripts\pytest.exe
```

Frontend build:

```powershell
cd frontend
npm run build
```

Environment check:

```powershell
.\scripts\check-environment.ps1
```

## Data And Secret Hygiene

Do not commit:

- `.env` files
- Python virtual environments
- Node modules
- runtime logs
- downloaded model weights
- local wiki dumps
- database exports
- machine-specific paths

The repository includes `.env.example` only as a placeholder contract.
