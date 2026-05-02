# HexaLogic

<p align="center">
  <img src="api/static/hexlogic-logo.png" width="180" alt="HexaLogic Logo"/>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="MIT License" />
  <img src="https://img.shields.io/badge/backend-Flask-black" alt="Flask Backend" />
  <img src="https://img.shields.io/badge/frontend-Netlify-00C7B7" alt="Netlify Frontend" />
  <img src="https://img.shields.io/badge/backend%20hosted-Render-46E3B7" alt="Render Backend Hosting" />
</p>

Professional multi-architecture emulator platform with a high-fidelity 8051 core, a functional/minimal ARM core, assembler pipelines, backend debugging, live memory/register views, breakpoints, and an IDE-style interface.

---

## Live Deployment

Frontend (Netlify CDN):  
https://hexalogic.netlify.app

Backend API (Render Web Service):  
https://hexalogic.onrender.com

---

## Overview

HexaLogic is a web-based emulator platform centered on AT89C51 behavior and extensible CPU architecture support.

It provides:

- Assembly compilation
- Step-by-step execution
- PC-driven fetch/decode/execute simulation
- Internal RAM and Code ROM views
- Register + SFR inspection
- Breakpoints
- Memory editing
- Base conversion utilities
- Exportable debug snapshot
- Functional/minimal ARM execution path with endian switching (not cycle-accurate)
- Redis-ready isolated session storage

Designed for:

- Embedded systems students
- Microcontroller lab practice
- Teaching environments
- Browser-based debugging without hardware

---

## Architecture

```text
Monaco / Incremental Debug UI
            |
            | REST API (JSON)
            v
SessionStore (memory / Redis)
            |
            v
CPU / Assembler Factory
      |               |
      v               v
    8051             ARM
            |
            v
Virtual Memory + Debugger + Peripherals
```

### Frontend

- HTML/CSS/JavaScript
- IDE-style UI
- Editor with breakpoint gutter
- Debug controls

### Backend

- Flask REST API
- Python-based multi-architecture simulation core
- 8051 PC-driven emulator engine
- Functional/minimal ARM emulator engine with approximate timing (not cycle-accurate)
- ROM / IRAM / SFR / XRAM / endian-aware memory model
- Session-isolated debugger state and observability hooks

---

## Features

- AT89C51 simulation flow
- ARM sandbox execution mode
- Assemble / Run / Step Into / Step Over / Step Out / Pause / Stop / Run-to-Cursor
- Reverse step / time-travel debugging
- Current instruction highlighting
- Live internal RAM viewer
- Code ROM view
- XRAM view
- Register bank table
- SFR + flag watch panel
- Breakpoint support
- Watchpoint support
- Trace timeline and call stack view
- Session export / import
- Plugin-ready architecture registry
- Memory edit
- Hex/Dec/Bin base converter
- Full simulation state export (JSON)
- In-app help panel
- Redis-ready session backend
- Docker deployment path
- Versioned session / API payloads

---

## Project Structure

```text
HEXALOGIC/
├── index.html             # Vite HTML entry (built to dist/ for Netlify)
├── src/                   # Frontend boot entry (Vite)
├── package.json           # Frontend build tooling (Vite)
├── vite.config.js         # Vite build config (outputs dist/)
├── scripts/               # Build helpers (static asset copy)
├── dist/                  # Build output (Netlify publish dir)
├── netlify.toml           # Netlify build + proxy redirects
├── render.yaml            # Render service blueprint
├── api/
│   ├── index.py            # Flask entry point
│   ├── templates/          # UI templates
│   └── static/             # CSS/JS/images/logo assets
├── core/                   # Legacy simulation engine
├── sim8051/                # Multi-architecture emulator runtime
├── tests/                  # Pytest test suite
├── .github/workflows/      # CI workflows
├── Dockerfile
├── requirements.txt
└── README.md
```

---

## Local Development

### 1. Clone

```bash
git clone https://github.com/AshwinderPalSingh/HEXALOGIC.git
cd HEXALOGIC
```

### 2. Create Virtual Environment

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 3. Install

```bash
pip install -e .
```

### 4. Run Locally

Option A uses the Flask CLI:

```bash
flask --app api/index.py run --debug
```

Option B uses the bundled local Flask launcher:

```bash
python scripts/run_local_flask.py
```

Optional host/port override:

```bash
HOST=0.0.0.0 PORT=8080 python scripts/run_local_flask.py
```

### 5. Open Local Server

```text
http://127.0.0.1:5000
```

---

## Testing

Run full test suite:

```bash
.venv/bin/python -m pytest -q
```

### Hardware Validation

Run the reproducible hardware audit used for 8051 GPIO/timer/interrupt checks plus ARM functional MMIO coverage:

```bash
.venv/bin/python validate_hardware.py
```

Emit machine-readable JSON instead of the default Markdown report:

```bash
.venv/bin/python validate_hardware.py --format json
```

The browser Runtime Metrics panel now exposes UI timing telemetry including receive-to-paint latency, server-to-paint latency, frame gaps, and dropped-frame counts so hardware visualization lag can be measured directly during interactive runs.

---

## Production Deployment

### Frontend - Netlify

- Uses Vite build output (`dist/`) as the publish directory
- `netlify.toml` proxies `/backend/*` to Render API
- Auto-deploy on push from GitHub
- Local build: `npm run build` (outputs `dist/`)

### Backend - Render

- Create service from this repository
- `render.yaml` already defines build/start/health config
- Gunicorn start command: `gunicorn wsgi:app`
- CORS controlled by `CORS_ALLOWED_ORIGINS`
- Optional Redis persistence through `HEXLOGIC_SESSION_BACKEND=redis` and `REDIS_URL`

### Container Deployment

```bash
docker build -t hexalogic .
docker run --rm -p 8080:8080 hexalogic
```

---

## Environment Variables (Render)

Example:

```bash
FLASK_ENV=production
CORS_ALLOWED_ORIGINS=https://hexalogic.netlify.app
HEXLOGIC_API_BASE=
HEXLOGIC_SESSION_BACKEND=memory
REDIS_URL=
```

---

## Contact

Help / Support:  
ashwinder.p.prof@gmail.com

---

## License

MIT License. See [LICENSE](LICENSE).
