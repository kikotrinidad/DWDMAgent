# DWDM Agent

A production-focused DWDM monitoring and reporting agent for Cisco NCS 2000/2006 networks.

This project polls DWDM nodes over TL1, stores raw + structured telemetry in PostgreSQL, runs AI analysis per node, and generates an operations-ready HTML report.

## What This Does

- Polls multiple DWDM nodes using TL1 command workflows
- Archives raw responses for traceability
- Parses and stores alarms, conditions, inventory, topology, and optical power
- Runs AI analysis per node for health scoring and actionable priorities
- Builds network report outputs for NOC and management review
- Supports parallel orchestration for faster full-network runs

## Project Layout

- `config.py` : shared environment/config loader
- `DWDMAgent/dwdm_agent.py` : central orchestrator (multi-process)
- `DWDMAgent/node_poller.py` : standalone poller agent for a single node
- `DWDMAgent/ai_agent.py` : standalone AI analysis agent
- `DWDMAgent/ai_analyst.py` : Gemini prompt + response parsing logic
- `DWDMAgent/parsers.py` : TL1 response parsers
- `DWDMAgent/db.py` : PostgreSQL persistence layer
- `DWDMAgent/report_builder.py` : per-run report generation
- `DWDMAgent/network_report.py` : network-level PM/Executive report flow
- `DWDMAgent/schema.sql` : database schema
- `DWDMAgent/setup_db.sh` : DB bootstrap helper

## Architecture

1. Phase 1 - Node Polling
- Pollers connect via TL1 (`port 3082`), execute command stack, and persist data

2. Phase 2 - AI Analysis
- Per-node summary is analyzed and stored as structured briefing JSON + HTML snippet

3. Phase 3 - Report Build
- Aggregates node outputs and writes final HTML report artifacts

## Requirements

- Python 3.10+
- PostgreSQL 14+
- Network access to DWDM nodes
- Gemini API access (via environment variable)

Python packages:

```bash
pip3 install psycopg2-binary google-genai
```

## Configuration

All runtime configuration is loaded from `.env` at repository root.

Expected categories:
- DB connection settings (`DWDM_DB_*`)
- Gemini settings (`GEMINI_*`, `AI_MAX_*`)
- Node blocks (`DWDM_<NODE>_IP`, `_PORT`, `_TID`, `_SITE`, `_USER`, `_PASS`)

Important:
- `.env` is intentionally not tracked by git.
- Do not hardcode credentials in source files.

## Database Setup

```bash
sudo -u postgres bash /opt/AIExperts/DWDMAgent/setup_db.sh
```

Or apply schema manually from `DWDMAgent/schema.sql`.

## Run Commands

Full run:

```bash
python3 /opt/AIExperts/DWDMAgent/dwdm_agent.py
```

Single node:

```bash
python3 /opt/AIExperts/DWDMAgent/dwdm_agent.py --node <TID>
```

Adjust process concurrency:

```bash
python3 /opt/AIExperts/DWDMAgent/dwdm_agent.py --pollers 8 --ai-agents 4
```

Standalone poller only:

```bash
python3 /opt/AIExperts/DWDMAgent/node_poller.py --tid <TID>
```

Standalone AI only:

```bash
python3 /opt/AIExperts/DWDMAgent/ai_agent.py --tid <TID>
```

## Reports and Logs

- Logs: `DWDMAgent/logs/`
- Reports: `DWDMAgent/reports/`

These folders are ignored for git upload by default.

## Git Upload Policy in This Repo

Tracked:
- Source code
- SQL schema
- setup scripts
- Root `README.md`

Ignored:
- `.env`
- Other `*.md` files
- Reports, sample outputs, logs
- `.vscode` and Python cache files
- Local systemd unit templates
- Experimental compare/qwen scripts

## Security Notes

- Keep API keys and device credentials only in `.env`
- Rotate keys immediately if accidentally exposed
- Review staged changes with `git status` before push

## License

No license file is currently included.
Add one before wider distribution if needed.
