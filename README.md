# Spedicija App

AI-powered freight forwarding assistant for BiH customs declarations. Monitors Gmail for invoices, extracts shipment data, classifies goods against the BiH customs tariff (2026), and generates ASYCUDA-compatible SAD XML.

## Stack

| Layer | Tech |
|---|---|
| Backend | FastAPI + SQLite (SQLAlchemy) + ChromaDB |
| Frontend | Vanilla JS SPA |
| AI | Claude Sonnet 4.6 (extraction, Step 2) · Haiku 4.5 (Step 1, LLM gate) |

## Features

- **Gmail pipeline** — polls inbox, detects shipment emails (LLM gate), extracts invoice data
- **Tariff classification** — batch AI classification against BiH 2026 tariff; corrections DB feeds vectorstore for future lookups
- **SAD editor** — SAD form overlay on official NERMUS PDF at 300 DPI
- **ASYCUDA XML** — generates valid SAD XML for customs submission
- **Dashboard** — queue overview, knowledge base browser, automation controls, detection audit log, Claude cost tracking

## Quick start

```bash
cp .env.example .env          # add ANTHROPIC_API_KEY
pip install -r backend/requirements.txt
./dev.sh                      # starts FastAPI on :8000
```

Open `http://localhost:8000/dashboard.html`

## Environment

```
ANTHROPIC_API_KEY=sk-ant-...
```

Google credentials (`credentials.json`) and OAuth token (`data/token.json`) required for Gmail integration — see `backend/google_auth.py`.

## Project layout

```
backend/
  main.py              FastAPI endpoints
  pipeline.py          Gmail → extract → finalize flow
  claude_classifier.py Tariff classification (batch AI + corrections)
  xml_generator.py     ASYCUDA XML generation
  database.py          SQLAlchemy models + migrations
  gmail_watcher.py     Gmail polling + LLM gate
frontend/
  dashboard.html       Main SPA (Pregled, Radni prostor, Baza znanja, Automatizacija, Detekcija, Postavke)
  sad_editor.html      SAD form editor
data/
  spedicija.db         SQLite database (gitignored)
```

## PendingShipment lifecycle

```
pending → extracted → uploaded   (semi-auto, 3-step)
pending → approved               (legacy, one-shot)
```

## Tests

```bash
pytest backend/test_xml_generator_edge_cases.py      # 42 tests
pytest backend/test_claude_classifier_edge_cases.py  # 38 tests (requires anthropic SDK)
```
