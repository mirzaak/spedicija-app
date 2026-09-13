# Spedicija App — CLAUDE.md

## Output stil
- Kratki odgovori. Bez preambula, bez recap-a.
- Kod direktno — ne objašnjavaj šta kod radi ako su nazivi jasni.
- Izvještaj: samo što se promijenilo + šta slijedi. Jedna-dvije rečenice.
- Ne ponavljaj informacije koje su već poznate iz konteksta.

## Stack
- Backend: FastAPI + SQLite (SQLAlchemy) + ChromaDB, `backend/`
- Frontend: Vanilla JS, `frontend/`
- AI: Claude Sonnet 4.6 (ekstrakcija + Step 2), Haiku 4.5 (Step 1)
- Pokretanje: `./dev.sh` ili `python -m uvicorn backend.main:app --reload`
- DB: `data/spedicija.db`

## Arhitektura
- `backend/main.py` — FastAPI endpoints
- `backend/pipeline.py` — Gmail→extract→finalize tok
- `backend/claude_classifier.py` — tarifna klasifikacija
- `backend/xml_generator.py` — ASYCUDA XML generacija
- `backend/tariff_pdf_parser.py` — BiH carinska tarifa import
- `backend/database.py` — SQLAlchemy modeli + migracije
- `frontend/index.html` — glavni UI
- `frontend/sad_editor.html` — SAD obrazac editor (PNG overlay)
- `frontend/sad_background.png` — NERMUS SAD PDF @ 300 DPI

## PendingShipment lifecycle
`pending → extracted → uploaded` (semi-auto)
`pending → approved` (legacy jednim potezom)

## Pravila
- Ne diraj Drive bez agentove eksplicitne potvrde.
- `_migrate()` u database.py za sve nove kolone (ALTER TABLE).
- Prompt caching: `cache_control: ephemeral` na dugim sistemskim promptovima.
- `tariff_from_invoice: true` samo ako je kod eksplicitno u fakturi.
- Backend restart obavezan nakon izmjena ruta.

## Three Man Team
Available agents: Arch (Architect), Bob (Builder), Richard (Reviewer)

## graphify

This project has a knowledge graph at graphify-out/ with god nodes, community structure, and cross-file relationships.

Rules:
- For codebase questions, first run `graphify query "<question>"` when graphify-out/graph.json exists. Use `graphify path "<A>" "<B>"` for relationships and `graphify explain "<concept>"` for focused concepts. These return a scoped subgraph, usually much smaller than GRAPH_REPORT.md or raw grep output.
- If graphify-out/wiki/index.md exists, use it for broad navigation instead of raw source browsing.
- Read graphify-out/GRAPH_REPORT.md only for broad architecture review or when query/path/explain do not surface enough context.
- After modifying code, run `graphify update .` to keep the graph current (AST-only, no API cost).
