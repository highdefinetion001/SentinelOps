# SentinelOps Dashboard

React dashboard for the ConfigGuard API in this repository. The UI uses live fleet state and on-demand detection scans; it does not fall back to sample server or finding data.

## Run locally

1. From the repository root, start the API and simulated fleet:

   ```bash
   cd /home/ishvak/Documents/projects/SentinelOps
   docker compose up --build -d --wait
   ```

2. In this directory, install the dependencies from `package-lock.json` and start Vite:

   ```bash
   cd frontend
   cp .env.example .env.local
   npm ci
   npm run dev
   ```

   The Vite `/api` proxy defaults to `http://127.0.0.1:8000`. Set `CONFIGGUARD_API_TARGET` in `.env.local` if the backend uses another address.

3. Open the Vite URL printed in the terminal. The dashboard requests `/api/health`, `/api/fleet/states`, and `POST /api/scans`. Server details also expose the allowlisted demo drift and reset endpoints; those controls require `DEMO_MODE=true` in the backend environment.

Peer analysis is supplementary. A collection failure is shown as unreachable, not as configuration drift. CPU, memory, and host IP are not supplied by the API and are not fabricated.
