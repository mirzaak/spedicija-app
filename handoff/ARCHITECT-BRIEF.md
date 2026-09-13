# Architect Brief

## Step 1 — Backend stabilizacija + praćenje troškova

Cilj: popraviti poznate bugove i dodati perzistenciju Claude token potrošnje + agregatni endpoint za budući dashboard. Backend-only korak. Bez frontend izmjena.

### 1A — Bugfixevi u `backend/main.py`

- **Bug 1 — `/pipeline/finalize` Gmail marking** (~L996-1003, unutar `_run()` u `finalize_shipment`):
  `mark_gmail_processed()` se trenutno poziva bezuslovno nakon DB bloka. Premjesti ga tako da se izvrši **samo ako `result["status"] == "ok"`**. Ako finalize padne, email NE smije biti markiran processed.

- **Bug 2 — `/pipeline/approve` prerani commit** (L813-814):
  Trenutno `row.status = "approved"; db.commit()` prije daemon threada → ako `process_shipment()` baci, pošiljka trajno zaglavi na "approved" bez XML/Drive. Refaktoriši da slijedi ISPRAVAN obrazac iz `pipeline.py` `run_pipeline()` (L915-948): status ostaje `pending`, cijeli rezultat (status→"approved" + polja) se piše u JEDNOJ transakciji TEK nakon uspješnog `process_shipment()`. Ako padne, red ostaje `pending`.

- **Bug 3 — `/chat/assist` bez error handlinga** (~L1679-1700):
  `client.messages.create(...)` obmotaj u try/except; kod greške vrati `raise HTTPException(500, f"Chat greška: {e}")`. Isti obrazac kao ostali Claude pozivi.

- **CORS** (L35-40): promijeni `allow_origins=["*"]` na `["http://localhost:8000", "http://127.0.0.1:8000"]`.

### 1B — Praćenje troškova

- Nova tabela `ApiUsage` u `backend/database.py`:
  `id (PK), ts (DateTime default utcnow), model (String), context (String — npr. "extract","classify","chat_assist","chat_declaration"), input_tokens (Int), output_tokens (Int), cache_read (Int), cache_write (Int), cost_est (Float)`.
  Nova tabela → `Base.metadata.create_all()` u `init_db()` je dovoljan. **NE dodavati u `_migrate()`** (to je samo za ALTER na postojećim tabelama).

- Helper `log_api_usage(db, model, context, usage)` u `database.py`:
  Čita `usage.input_tokens`, `usage.output_tokens`, `getattr(usage,'cache_read_input_tokens',0)`, `getattr(usage,'cache_creation_input_tokens',0)`.
  Računa `cost_est` iz per-model cjenovnika (dict konstanti na vrhu fajla za `claude-sonnet-4-6` i `claude-haiku-4-5-20251001`: input/output/cache_read/cache_write USD po milion tokena).
  Flag: **Potvrdi AKTUELNE cijene kroz `claude-api` skill prije hardkodiranja — ne pogađaj.**
  Otvori vlastitu sesiju ili primi `db`; commit-aj usage red nezavisno (usage logging ne smije rušiti glavni tok — obmotaj u try/except, greška samo warning).

- Pozovi `log_api_usage` na 3 mjesta gdje `message.usage` već postoji:
  - `pipeline.py` `_analyze_shipment_with_claude` (~L295, nakon `message = client.messages.create`), context="extract".
  - `claude_classifier.py` — nakon Claude poziva (nađi `messages.create`), context="classify".
  - `main.py` `/chat/assist` (context="chat_assist") i `/pipeline/chat` (context="chat_declaration").

### 1C — Endpointi

- `GET /usage/stats`: agregira `ApiUsage` — `{today, last_7d, total}` svaki sa `{cost, input_tokens, output_tokens, by_model:{...}}`.
- `GET /dashboard/summary`: objedini postojeće brojače u jedan JSON. Reuse upite iz:
  `/pipeline/stats` (L1207), `/knowledge/stats` (L1165), `/tariffs/corrections/stats` (L1615), plus queue brojači (pending/extracted/uploaded/skipped iz PendingShipment), suspect/empty_codes agregat, i `/usage/stats` sažetak.

### Flags (ne pogađaj)
- Cijene modela → potvrdi kroz `claude-api` skill.
- Ne diraj frontend u ovom koraku.
- Ne diraj `auto_extract_eligible` (trajno neaktivno, namjerno).
- Backend restart obavezan nakon izmjena ruta (CLAUDE.md).

### Kad završiš
- Update `handoff/BUILD-LOG.md` + napiši `handoff/REVIEW-REQUEST.md` sa fajlovima+linijama, `Ready for Review: YES`.
