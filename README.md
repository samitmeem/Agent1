# Ticket Assistant

> Built on [agent-foundation](https://github.com/samitmeem/Agent-Platform) —
> a layered foundation that turns a `plan.md` into agent-driven execution
> with a validation gate.

Production ticket-assistant system: Vite/React client, FastAPI Gateway with
WebSocket fan-out, PostgreSQL + Redis/ARQ, LangGraph worker with LiteLLM
routing (Qwen-first, DeepSeek fallback), and a voice service (Faster-Whisper
STT + Kokoro TTS).

The full architecture specification lives in [`AgenticProject.md`](./AgenticProject.md).
The executable task breakdown lives in [`.foundation/plan/plan.md`](./.foundation/plan/plan.md).

## Structure

```
Agent1/
├── .foundation/      ← agent-foundation tooling (dev-only, never deploy)
│   ├── .devcontainer/
│   ├── .github/
│   ├── .vscode/
│   ├── auto-runner/
│   ├── seam/
│   ├── validation/
│   ├── validation-mcp/
│   ├── vendor/       ← 3 submodules (agent-platform, token-savior, context-mode)
│   ├── plan/         ← plan.md lives here
│   ├── bootstrap.sh
│   ├── package.json
│   └── tsconfig.json
├── AgenticProject.md ← system specification (source of truth)
├── .gitignore
└── README.md         ← (this file)
```

Application code (gateway/, worker/, llm/, voice/, frontend/, database/,
deploy/) is created at the project root by the plan tasks — alongside
`.foundation/`, not inside it. The foundation is dev-only tooling and never
ships to production.

## Quick start

### 1. Open in VS Code (devcontainer recommended)
1. Install the **Dev Containers** extension.
2. Open this folder. VS Code discovers `.foundation/.devcontainer/devcontainer.json`.
3. **Reopen in Container**. `bootstrap.sh` runs: installs deps, builds
   Agent-Platform + auto-runner, installs both as VSIX, indexes the plan into
   context-mode's FTS5 store.
4. Reload the window. `@agent-platform` and the auto-runner commands are available.

### 2. Run the plan (the simple way)
1. Edit `.foundation/plan/plan.md` — already populated from `AgenticProject.md`.
2. Command Palette (`Ctrl+Shift+P`) → **Agent Foundation: Run Plan**
   - Sends Task 1 to `@agent-platform` in Copilot Chat.
   - After the agent finishes, prompts you to validate.
   - On pass → auto-advances to Task 2. On fail → stops.
3. To resume after a stop: **Agent Foundation: Run Next Task**.
4. To validate the current task manually: **Agent Foundation: Validate Current Task**.

### 3. Run the plan (manual / full control)
```bash
# Dry-run: print the commands that would be sent to @agent-platform
npx tsx .foundation/seam/plan-loader.ts .foundation/plan/plan.md --dry-run

# Live: emit one command per task
npx tsx .foundation/seam/plan-loader.ts .foundation/plan/plan.md

# Resume from a specific task
npx tsx .foundation/seam/plan-loader.ts .foundation/plan/plan.md --from-task 4

# Validate a completed task
npx tsx .foundation/validation/runner.ts --plan .foundation/plan/plan.md --task 2
```

## Retrieval mode

This plan uses `retrieval: indexed` because the spec is large. The plan and
`AgenticProject.md` are indexed into context-mode's FTS5 store. The agent uses
`ctx_search` to retrieve relevant sections per task instead of loading the
whole spec into context. To check cost-optimal mode for a different plan size:

```bash
npx tsx .foundation/seam/measure.ts .foundation/plan/plan.md
```

## How agent-foundation is wired into this project

- **Foundation tooling** lives in `.foundation/` (copied from
  `agent-foundation` via `seam/new-project.ts`). It is dev-only.
- **The spec** (`AgenticProject.md`) stays at the project root as the source of
  truth. It is referenced from the plan's Context section and indexed for
  on-demand retrieval.
- **The plan** (`.foundation/plan/plan.md`) is the executable task graph —
  12 tasks derived from the spec's Developer Task Breakdown, each with
  acceptance criteria the validation gate checks.
- **Routing** is enforced two ways: `.foundation/.github/copilot-instructions.md`
  (instruction layer) and `.foundation/seam/merged-hooks.json` (hook layer).
  External output → context-mode; codebase nav → token-savior; memory →
  token-savior; plan retrieval → context-mode.

## Services

Application code lives at the project root, alongside `.foundation/`. Each
service is independently runnable.

| Service | Folder | Stack | Port |
|---|---|---|---|
| Gateway | `gateway/` | FastAPI (Python 3.11) | 8000 |
| Worker | `worker/` | ARQ + LangGraph (Python 3.11) | — |
| LLM router | `llm/` | LiteLLM (Python 3.11) | — |
| Voice | `voice/` | Faster-Whisper + Kokoro (Python 3.11) | 8000 |
| Frontend | `frontend/` | Vite + React + TypeScript | 5173 |
| Database | `database/` | PostgreSQL + Alembic | 5432 |
| Deploy | `deploy/` | docker-compose | — |

### Start each service

#### 1. Postgres + Redis (via docker compose)
```bash
docker compose -f deploy/docker-compose.yml up -d postgres redis
```

#### 2. Gateway
```bash
cd gateway
python -m venv .venv && source .venv/bin/activate
pip install -e .
uvicorn app.main:app --reload --port 8000
# health check
curl http://localhost:8000/healthz   # -> {"status":"ok"}
```

#### 3. Worker
```bash
cd worker
python -m venv .venv && source .venv/bin/activate
pip install -e .
arq worker.jobs.WorkerSettings
```

#### 4. LLM router (used by the worker)
```bash
cd llm
python -m venv .venv && source .venv/bin/activate
pip install -e .
```

#### 5. Voice service
```bash
cd voice
python -m venv .venv && source .venv/bin/activate
pip install -e .
uvicorn voice.app.main:app --port 8000
```

#### 6. Frontend
```bash
cd frontend
npm install
npm run dev          # http://localhost:5173
```

#### 7. Full stack (all services via compose)
```bash
docker compose -f deploy/docker-compose.yml up -d
```

Each service folder has a `.env.example` — copy to `.env` and adjust before
running outside of compose.

## Deployment

Only deploy application folders (`gateway/`, `worker/`, `llm/`, `voice/`,
`frontend/`, `database/`, `deploy/`). The `.foundation/` folder is dev-only
tooling and should never ship to production.
