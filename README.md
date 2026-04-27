# Milma_Malappuram

Industrial dashboard for PPI AIME 8U modules via Modbus TCP. Features a high-speed FastAPI backend & SQLite logging on Radxa ROCK 5C. Provides 1Hz live monitoring (WebSockets), historical trends (uPlot), and Excel export. Lightweight, Docker-ready, and optimized for low-resource edge deployment.

## Demo
<p align="center">
  <img src="https://github.com/user-attachments/assets/73f302e5-7978-4826-ac0b-2a1b0830afc3" width="30%" />
  <img src="https://github.com/user-attachments/assets/512dc1fa-2553-437b-831a-ec2c97c7f8e9" width="30%" />
  <img src="https://github.com/user-attachments/assets/397cdd9f-c527-434e-9b64-a5e3d07cc7e7" width="30%" />
  <img src="assets/gif1.mov" width="30%" />
</p>

# Milma Malappuram — AIME 8U Dashboard

Web-based monitoring dashboard for PPI **AIME 8U** 8-channel analog input
RTUs over Modbus TCP/IP. Polls every configured RTU, stores readings in
local SQLite, and serves a live view + historical trends + history table
+ Excel export.

**Target device:** Radxa ROCK 5C (arm64, 2 GB RAM, 128 GB SSD), Linux + Docker.

See [CLAUDE.md](CLAUDE.md) for the full architecture, register map,
schema, and performance budget. Edit `backend/devices.yaml` to declare
RTUs and channels.

---

## Quickstart

```bash
# 1. Copy the env template and adjust if needed.
cp .env.example .env

# 2. Build the backend image and start the container.
docker compose up -d --build

# 3. Tail the logs (should see poll cycles ticking once a second).
docker compose logs -f backend

# 4. Hit the health endpoint.
curl http://localhost:8000/api/health

# 5. Open the live dashboard in a browser.
#    http://localhost:8000    (or http://<radxa-ip>:8000 from another host)
```

## Common commands

```bash
# Validate compose file without building
docker compose config

# Rebuild just the image (no restart)
docker compose build

# Inspect the final image size (should be < 200 MB)
docker compose images

# Shell into the container
docker compose exec backend sh

# SQLite shell against the live DB
docker compose exec backend sqlite3 /data/aime.db

# Run tests
docker compose exec backend pytest -q

# Atomic backup while running
docker compose exec backend sqlite3 /data/aime.db \
    ".backup /data/backups/aime-$(date +%Y%m%d).db"
```

## Layout

```
milma-dashboard/
├── CLAUDE.md              # architecture, register map, schema, budget
├── README.md              # this file
├── docker-compose.yml
├── .env.example           # copy to .env
├── .gitignore
├── docs/
│   └── AIME-8U-Manual.pdf
├── reference/
│   └── aime8u_modbus_reader.py   # known-working single-shot reader
└── backend/
    ├── Dockerfile         # multi-stage, arm64, final image < 200 MB
    ├── pyproject.toml     # pinned deps (fastapi, pymodbus, …)
    ├── devices.yaml       # RTU + channel config (source of truth)
    └── app/               # FastAPI app — filled in by later phases
```

## Frontend vendor assets (pinned)

Downloaded during `docker build` into `app/static/vendor/` — no CDN at
runtime. To bump a version, edit `backend/Dockerfile` and rebuild.

| Asset            | Version | Source                                                    |
|------------------|---------|-----------------------------------------------------------|
| htmx.min.js      | 2.0.4   | unpkg.com/htmx.org@2.0.4/dist/htmx.min.js                 |
| alpine.min.js    | 3.14.8  | cdn.jsdelivr.net/npm/alpinejs@3.14.8/dist/cdn.min.js      |
| uplot.iife.min.js| 1.6.32  | cdn.jsdelivr.net/npm/uplot@1.6.32/dist/uPlot.iife.min.js  |
| uplot.min.css    | 1.6.32  | cdn.jsdelivr.net/npm/uplot@1.6.32/dist/uPlot.min.css      |
| pico.min.css     | 2.0.6   | cdn.jsdelivr.net/npm/@picocss/pico@2.0.6/css/pico.min.css |

## Optional: reverse proxy with HTTPS + basic auth

Uncomment the `caddy` service in `docker-compose.yml`, fill in
`DASHBOARD_DOMAIN`, `DASHBOARD_USER`, and `DASHBOARD_PASSWORD_HASH` in
`.env` (see `.env.example`), and remove the backend's `ports:` block so
the app is only reachable via Caddy on 80/443. Caddy will obtain a
Let's Encrypt certificate automatically if the domain resolves to the host.

## Hardware notes

- One persistent Modbus TCP connection per RTU. AIME 8U allows at most
  2 concurrent clients — do not run multiple poller processes.
- Default poll rate is 1 Hz. Do not exceed unless you've lowered the
  device's on-board sample time — otherwise you read the same ADC
  conversion twice.
- SQLite runs in WAL mode with 4 MB page cache and 64 MB mmap. The
  downsampler checkpoints the WAL hourly so it can't grow unbounded.
