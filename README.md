# ConfigGuard AI

Local configuration drift demo: three isolated server containers expose validated state to a FastAPI backend over a private Compose network. The backend compares collected states deterministically with the approved baseline, applies YAML policy severities, and reports peer similarity as supplementary evidence. Scan responses are returned on demand and are not persisted yet.

## Start

Requirements: Python 3.11–3.12 for local development, Docker Engine, and Docker Compose v2.

```bash
cp .env.example .env
docker compose up --build -d --wait
```

The backend listens on `http://localhost:8000`. The simulated servers have no published host ports. `DEMO_MODE=true` in the example `.env` enables the predefined controls. Set it to `false` and recreate the services to disable them.

```bash
curl http://localhost:8000/api/health
curl http://localhost:8000/api/fleet/states
curl -X POST http://localhost:8000/api/scans
curl -X POST http://localhost:8000/api/fleet/server-03/simulate-drift \
  -H 'Content-Type: application/json' -d '{"scenario":"security_drift"}'
curl http://localhost:8000/api/fleet/states
curl -X POST http://localhost:8000/api/scans
curl -X POST http://localhost:8000/api/fleet/server-03/reset
curl http://localhost:8000/api/fleet/states
```

To stop the demo, run `docker compose down`.

## Tests and local API

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
pytest
ruff check .
ruff format --check backend fleet tests
```

The Docker integration smoke test runs automatically when Docker Engine is reachable and Compose v2 are available. If the Docker CLI exists but the current user cannot access the daemon, pytest reports the integration test as skipped with that reason. To run only non-Docker tests, use `pytest -m 'not integration'`.

`POST /api/scans` performs an on-demand collection and detection pass. Scan history, alert lifecycle state, persistence, automatic scheduling, AI explanations, and the dashboard are later phases.

## Dashboard

The React dashboard from the companion SentinelOps project now lives in `frontend/` and uses this repository's FastAPI backend. It calls `GET /api/health`, `GET /api/fleet/states`, `POST /api/scans`, `POST /api/fleet/{server_id}/simulate-drift`, and `POST /api/fleet/{server_id}/reset`. The Vite development proxy sends these `/api` requests to `http://127.0.0.1:8000` by default. Start the API and fleet as above, then in another terminal run:

```bash
cd frontend
cp .env.example .env.local
npm ci
npm run dev
```

Open the Vite URL printed in the terminal and use the demo login: any Gmail-formatted address and password `123`. This is client-side demo gating only, not real authentication. After login, the landing page links to `/dashboard`. Set `CONFIGGUARD_API_TARGET` in `frontend/.env.local` if the API listens elsewhere. Demo drift and reset actions require `DEMO_MODE=true` in the backend environment.

## Repository layout

```text
SentinelOps/
├── backend/                 FastAPI app, API schemas, collection, policy and detection
│   └── app/
│       ├── collector/       Server registry and HTTP collection
│       ├── config/          Baseline and policy loaders
│       ├── core/            Fingerprinting and deterministic drift detection
│       └── schemas/         Pydantic request, state and scan contracts
├── config/                  JSON Schema, approved baseline and YAML policy
├── fleet/                   Simulated server container and per-server configs
├── frontend/                React + Vite dashboard and API client
│   └── src/
│       ├── pages/            Login, landing page and fleet dashboard
│       ├── services/api.js   HTTP calls to the FastAPI endpoints
│       └── styles/           Page-specific styles
├── tests/                    Backend contract, API, detector and fleet tests
├── docker-compose.yml        Three simulated servers and the backend
├── pyproject.toml            Python dependencies and tool configuration
└── README.md                 Setup, validation and run instructions
```

The frontend is a client of the backend; it does not contain a second API server. Compose exposes only the backend on the host. The simulated servers stay on the internal Compose network.

For local backend development without the fleet, run `uvicorn backend.app.main:app --host 127.0.0.1 --port 8000`. Fleet collection then needs server URLs supplied in `FLEET_SERVERS`.
