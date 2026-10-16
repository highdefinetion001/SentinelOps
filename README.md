# ConfigGuard AI

Local configuration drift demo: a dedicated Monitor container collects validated state from three independently configured server containers over a private Compose network. The Monitor compares each server with its independently approved baseline, applies YAML policy severities, and reports peer similarity as supplementary evidence. Server inventory, baseline versions, scans, and findings persist in SQLite.

## Start

Requirements: Python 3.11–3.12 for local development, Docker Engine, and Docker Compose v2.

```bash
cp -n .env.example .env
docker compose up --build -d --wait
```

The Monitor listens on `http://localhost:8000`. The simulated servers have no published host ports. `DEMO_MODE=true` in the example `.env` enables the predefined demo drift and reset actions. Set it to `false` and recreate the services to disable them. SQLite data lives in the existing named `configguard_data` volume and survives container recreation and `docker compose down`; `docker compose down -v` removes the current Compose project's volumes.

The Monitor loads explicit server identities, roles, display names, and private endpoints from `config/servers.json`. Its startup collection creates a **pending** baseline candidate for each newly enrolled server; it never trusts that snapshot automatically. Approve one explicitly before drift comparisons can report clean or drifted results. For example:

```bash
candidate=$(curl -s http://localhost:8000/api/servers/server-03/baseline-candidates \
  | python3.12 -c 'import json,sys; print(json.load(sys.stdin)["items"][0]["baseline_id"])')
curl -X POST "http://localhost:8000/api/servers/server-03/baselines/$candidate/approve" \
  -H 'Content-Type: application/json' \
  -d '{"confirm":true,"approved_by":"demo-operator","reason":"Approved demo baseline"}'
```

The supplied actor is audit metadata only; this project does not yet authenticate baseline approvers. Approve a candidate for each server you want assessed. The dashboard includes this approval flow. `GET /api/scans?limit=20&offset=0` lists saved scans and `GET /api/scans/{scan_id}` returns a saved scan with its per-server baseline versions. The Monitor performs a startup scan and then scans at `SCAN_INTERVAL_SECONDS` (default 60); on-demand `POST /api/scans` shares the same scan lock.

Each server has its own named identity volume. The collector instance ID remains stable across a server restart; replacing the volume creates a different identity and the Monitor marks the mismatch instead of attaching the old baseline. The state endpoint returns only the versioned configuration contract. Collector identity, role, display name, and collector version travel in response headers and are checked against the declared registry. Collector endpoints are reachable only on the private Compose network. There is no per-caller collector authentication in this MVP: treat every workload attached to that network as trusted, and do not attach untrusted services or publish collector ports. Demo controls are separate allowlisted POST routes, disabled when `DEMO_MODE=false`.

To add a server, add its declaration to `config/servers.json`, add its Compose service and a unique identity volume, then recreate the Monitor and new server. It enters the pending-baseline workflow after a successful collection. Before removing a server, call `POST /api/servers/{server_id}/decommission`; removing its registry entry without decommissioning leaves its inventory and history visible as not configured. A changed role or endpoint for an existing ID becomes an identity conflict; enroll a replacement under a new ID rather than reusing its baseline.

## Tests and local API

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
pytest
ruff check .
ruff format --check backend fleet tests
```

The Docker integration smoke test runs automatically when Docker Engine is reachable and Compose v2 are available. It verifies all three collectors, explicit baseline approval, restart identity, persisted drift after a server restart, and inventory/baseline/scan history after Monitor restart. Its unique Compose project cleanup removes only its own resources. If the Docker CLI exists but the current user cannot access the daemon, pytest reports the integration test as skipped with that reason. To run only non-Docker tests, use `pytest -m 'not integration'`.

`POST /api/scans` performs and persists an on-demand collection and detection pass. Scan history is available at `GET /api/scans` and `GET /api/scans/{scan_id}`. `GET /api/health` reports process health only; `GET /api/ready` checks local database readiness for Compose and reports whether demo controls are enabled. The Monitor uses one Uvicorn worker while scheduling is enabled; startup fails clearly if `WEB_CONCURRENCY` requests multiple workers. Baseline approval has audit metadata but no real authentication in this MVP.

## Demonstration workflow

Start the Monitor and three servers with the Compose command above. In a new terminal, start the Next.js dashboard:

```bash
cd frontend
npm ci
npm run dev
```

Open <http://localhost:3000>. The dashboard proxies `/api/*` through Next.js to `http://127.0.0.1:8000`; set `CONFIGGUARD_API_TARGET` in the frontend environment if the Monitor is at a different address. The Compose `.env.example` enables predefined demo controls. The readiness response reports whether the Monitor has enabled them.

On a fresh database, wait for all three servers to appear as pending and review each snapshot in **Baseline enrollment**. Enter an audit actor name and approve each server's candidate explicitly. Run a scan and verify the fleet is clean. Under **Drift controls**, choose a predefined scenario for one server and confirm the action; the dashboard calls the real Monitor endpoint and runs a scan after the server acknowledges the change. Confirm the finding and severity under **Assessment details**. Reset that server, verify the reset acknowledgement, and inspect the follow-up scan for a clean result. **Scan history** shows persisted results and the baseline version used for every assessment.

The actor name and any client demo login are not authentication. Do not expose the development Monitor or demo controls to an untrusted network. To stop the demo while retaining its database, run `docker compose down` from the repository root. Do not use `docker compose down -v` when you want to keep baseline and scan history.

## Repository layout

```text
SentinelOps/
├── backend/                 FastAPI app, API schemas, collection, policy and detection
│   └── app/
│       ├── collector/       Server registry and HTTP collection
│       ├── config/          Baseline and policy loaders
│       ├── core/            Fingerprinting and deterministic drift detection
│       ├── schemas/         Pydantic request, state and scan contracts
│       └── storage/         Versioned SQLite inventory, baselines and scan history
├── config/                  JSON Schema, approved baseline and YAML policy
├── fleet/                   Simulated server container and per-server configs
├── frontend/                Next.js dashboard and same-origin API client
│   ├── app/                 Live fleet dashboard and styling
│   ├── lib/api.ts           Validated endpoint calls and response types
│   └── next.config.ts       Development API proxy to the Monitor
├── tests/                    Backend contract, API, detector and fleet tests
├── docker-compose.yml        Monitor, three server roles, private network and volumes
├── pyproject.toml            Python dependencies and tool configuration
└── README.md                 Setup, validation and run instructions
```

The frontend is a client of the Monitor; it does not contain a second API server. Compose publishes only the Monitor API on the host, bound to loopback by default. The monitored servers stay on the internal Compose network.

For local backend development without the fleet, run `DATABASE_URL=sqlite:///./data/configguard.db uvicorn backend.app.main:app --host 127.0.0.1 --port 8000`. Fleet collection then needs server URLs supplied in `FLEET_SERVERS`.
