# Build Log — Spedicija App

## Step 1 — Backend stabilizacija + praćenje troškova
Status: COMPLETED (Bob, 2026-09-07)

### 1A — Bugfixevi (`backend/main.py`)
- **CORS** (L35-40): `allow_origins=["*"]` → `["http://localhost:8000","http://127.0.0.1:8000"]`
- **Bug 1 — `/pipeline/finalize`** (L994-1003): `mark_gmail_processed()` premješteno unutar `if result["status"] == "ok"` bloka — ne markira email ako finalize padne
- **Bug 2 — `/pipeline/approve`** (L813-814): uklonjen prerani `row.status="approved"; db.commit()` — status + sva polja pišu se u JEDNOJ transakciji TEK nakon uspješnog `process_shipment()`, pattern preuzet iz `run_pipeline()`
- **Bug 3 — `/chat/assist`** (L1696): `client.messages.create` obmotano u try/except → `HTTPException(500, f"Chat greška: {e}")`
- **Bug 3b — `/pipeline/chat`** (L1405): isti try/except pattern dodat

### 1B — Praćenje troškova (`backend/database.py`)
- Nova tabela `ApiUsage` (id, ts, model, context, input_tokens, output_tokens, cache_read, cache_write, cost_est) — kreira se kroz `create_all()`, nije u `_migrate()`
- `log_api_usage(db, model, context, usage)` helper — računa cost_est iz `_MODEL_PRICING` dict konstanti, commit-uje nezavisno, greška = samo warning
- Pricing konstante: sonnet-4-6 ($3/$15/$0.30/$3.75), haiku-4-5-20251001 ($0.80/$4/$0.08/$1.00) per MTok
- `log_api_usage` pozvan na 4 mjesta:
  - `pipeline.py` `_analyze_shipment_with_claude` — context="extract"
  - `claude_classifier.py` `_batch_select_chapters` — context="classify" (Haiku)
  - `claude_classifier.py` `_batch_select_codes` — context="classify" (Sonnet)
  - `main.py` `/chat/assist` — context="chat_assist"
  - `main.py` `/pipeline/chat` — context="chat_declaration"

### 1C — Novi endpointi (`backend/main.py`)
- `GET /usage/stats` → `{today, last_7d, total}` svaki sa `{cost, input_tokens, output_tokens, by_model}`
- `GET /dashboard/summary` → queue brojači (pending/extracted/uploaded/approved/skipped), knowledge (tariff_records, corrections, declarations), quality (suspect_items, empty_codes), usage (today/7d cost)

## Step 2 — Unified dashboard shell + Pregled pogled
Status: COMPLETED (Bob, 2026-09-07)

### Što je urađeno (`frontend/dashboard.html`)
- Rebuild u SPA s lijevim sidenav railom (56px collapsed / 200px expanded on hover)
- Sidenav: logo ikona + "Spedicija" tekst, 2 aktivna linka (Pregled, Radni prostor), 3 disabled linka (Baza znanja, Automatizacija, Postavke)
- View switching čistim JS — `classList.remove/add('active')`, `display` kontroliran kroz `.view { display:none } .view.active { display:flex }`
- **Pregled view** (L377–407): fetch `/dashboard/summary` on load + "Osvježi" dugme; 4 kartice u 2×2 gridu:
  - Red pošiljki: pending big broj (zeleno/amber/crveno), 4 pills (extracted/uploaded/approved/skipped)
  - Pouzdanost klasifikacije: suspect_items + empty_codes s bojom
  - Baza znanja: tariff_records big broj, 3 pills (corrections, high_conf, declarations)
  - Claude troškovi: today_cost_usd formatiran `$X.XXXXX`, 7d cost + today_calls
- **Radni prostor view**: cijeli originalni 3-koloni layout (lista deklaracija, SAD overlay editor, AI chat) prebačen netaknut
- Svi endpoint pozivi identični originalu — nula promjena u funkcionalnosti

## Step 3 — Baza znanja / Automatizacija / Postavke u dashboard shellu
Status: COMPLETED (Bob, 2026-09-07)

### Što je urađeno (`frontend/dashboard.html`)

**Sidenav** (L828–837):
- Uklonjen `class="disabled"` i dodat `data-view` atribut za sva 3 prethodno onemogućena linka
- Baza znanja → `data-view="baza-znanja"`, Automatizacija → `data-view="automatizacija"`, Postavke → `data-view="postavke"`

**CSS** (L647–814):
- Dodat komplet `.view-body`, `.v-card`, `.v-card-head`, `.v-card-title` — container/card pattern koji prati postojeći `.metric-card` stil
- `.v-log` — live log box (monospace, dark, scroll)
- `.v-pills`, `.v-pill.ok/.warn/.error` — status pills
- `.v-table-wrap`, `.v-table` — tabla wrapper koji prati postojeći `thead th` stil
- `.v-btn`, `.v-btn-primary`, `.v-btn-ghost`, `.v-btn-red`, `.v-btn-green` — dugmad
- `.v-form-grid`, `.v-field` — forma grid i labele
- `.v-live-dot` — animirani zeleni dot
- `.chapters-grid`, `.chapter-chip` — chapter browser chips
- `.v-search` — search input
- `.corr-row`, `.corr-item-name`, `.corr-code-val`, `.corr-confirms`, `.btn-del-x` — corrections lista

**VIEW: Baza znanja** (L998–1059):
- Korekcije AI: fetch `GET /tariffs/corrections?q=`, prikaz sa `corr-row` redovima, delete `DELETE /tariffs/corrections/{id}`
- Import baze znanja: dugme → `POST /knowledge/import`, status prikaz
- Carinska tarifa browser: stats iz `GET /tariffs/official-stats`, chapters chips iz `GET /tariffs/chapters`, drill-down → `GET /tariffs/official-browse?chapter=X` → v-table

**VIEW: Automatizacija** (L1062–1114):
- Gmail status: `GET /gmail/status` → connected/disconnected pill; Reset Ignored → `POST /gmail/reset-ignored`, Reauth → `POST /gmail/reauth`
- Pipeline status: `GET /pipeline/status` → v-pills za credentials/token/scheduler; "Pokreni sada" → `POST /pipeline/run-now`
- Live log: `GET /pipeline/log` → auto-refresh `setInterval(5000)` dok je view aktivan; `clearInterval` pri napuštanju viewa

**VIEW: Postavke** (L1117–1167):
- Form polja: declarant_name, declarant_id/code, office_code, office_name, gmail_filter, notify_email
- `GET /settings` na load, `PUT /settings` na save; inline status feedback

**JS** (L1936–2270):
- `_viewActivated(viewId)`: čisti auto-refresh interval kad se napusti automatizacija; poziva lazy-load za svaki view
- `patchNavListener()` IIFE: dodaje drugi click listener na sve `[data-view]` stavke (za side effects, bez diranja originalne view-switch logike)
- `loadBazaZnanja()`, `bzLoadCorrections()`, `bzDeleteCorrection()`, `bzImportKnowledge()`, `bzLoadTariffStats()`, `bzLoadChapters()`, `bzDrillChapter()`, `bzClearChapter()` — KB tab logika
- `loadAutomatizacija()`, `autoLoadGmailStatus()`, `autoLoadPipelineStatus()`, `autoRunPipeline()`, `autoRefreshLog()`, `autoResetIgnored()`, `autoGmailReauth()` — Automatizacija logika
- `psLoadSettings()`, `psSaveSettings()` — Postavke logika
- Svi `innerHTML` upisi prolaze kroz `escapeHtml()`, ID-evi kroz `Number()`, URL-ovi nisu stavljani u href bez provjere

## Detection recall — Step 1
Status: COMPLETED (Bob, 2026-09-07)

### 1A — Image invoice support
- `ALLOWED_MIME_TYPES` + `ALLOWED_EXTENSIONS` (`gmail_watcher.py`): added `.jpg/.jpeg/.png/.tiff/.tif/.webp` and corresponding MIME types; `.docx` deferred with TODO comment.
- `_is_shipment_email`: images treated as business attachments (same as PDF/XLS), routed to LLM gate.
- `_file_to_base64_doc` (`pipeline.py` L85-104): new `elif` branch returns `{"type":"image","source":{...}}` block for image extensions.
- `has_vision` flag (`pipeline.py` L171): now set for both `"document"` and `"image"` block types.

### 1B — LLM gate replaces hard subject-rejects
- `_is_shipment_email` return signature changed to `(bool, str, str)` — third value is `stage in {"rule","llm"}`.
- Hard-reject sender list narrowed to unambiguous SaaS/tech billing domains only; soft patterns (`noreply`, `notifications@`, `newsletter`) moved to `_SOFT_SENDER_SIGNALS`.
- Former `_HARD_SUBJECT_REJECT` keywords moved to `_SOFT_SUBJECT_SIGNALS` — no longer hard-reject.
- New logic: strong keyword → accept(rule); 2+ weak → accept(rule); any business attachment OR soft signal → LLM gate(llm); otherwise → reject(rule).
- `_run_llm_gate()` extracted helper: Haiku `max_tokens=5`, extended prompt covers import vs. utility/SaaS/admin, defaults ACCEPT on failure, logs token usage via `log_api_usage` (non-blocking).
- Single caller in `fetch_new_invoices` updated to unpack 3-tuple `is_shipment, reason, stage`.

### 1C — gmail_filter setting wired
- `fetch_new_invoices` now reads `get_setting(db, "gmail_filter")` on each poll.
- If non-empty, used as base query; otherwise defaults to `"has:attachment"`.
- Mandatory exclusions (`-label:{PENDING} -label:{PROCESSED} -label:{IGNORED}`) always appended.
- Effective query logged via `_cb(...)`.

## Detection recall — Step 2
Status: COMPLETED (Bob, 2026-09-07)

### 2A — `DetectionLog` table + helper (`backend/database.py`)
- New table `detection_log` (id, ts, gmail_message_id, subject, sender, attachment_names [JSON], decision, reason, stage) — created via `Base.metadata.create_all()`, NOT in `_migrate()`.
- `import json` added to module-level imports.
- `log_detection(db, gmail_message_id, subject, sender, attachment_names, decision, reason, stage)` helper — same try/except-never-crash pattern as `log_api_usage`, own session if db is None, commits independently.

### 2B — Decision logging in `backend/gmail_watcher.py`
- `log_detection` imported from `database`.
- 4 decision points now logged inside `_ingest_message`:
  1. **rejected** — filter rejected, reason+stage from `_is_shipment_email`.
  2. **no_files** — no valid PDF/XLS/image attachments, stage="rule".
  3. **duplicate** — attachment hash collision, reason includes shipment id, stage="rule".
  4. **accepted** — PendingShipment created, reason+stage from `_is_shipment_email`.

### 2C — `_ingest_message` refactor + endpoints
- Per-message body extracted into `_ingest_message(service, message, db, label_ids, _cb) -> str` — pure extraction, behavior identical. `label_ids` dict keys: "pending", "processed", "ignored". `fetch_new_invoices` loop now calls `_ingest_message` and increments `new_count` only when decision=="accepted".
- `GET /detection/log?limit=100` (`backend/main.py`): returns recent `DetectionLog` rows desc, attachment_names parsed to list.
- `POST /detection/requeue/{gmail_message_id}` (`backend/main.py`): removes Ignored label, fetches full message, calls `_ingest_message`, returns `{decision, message}`.

## Detection recall — Step 3
Status: COMPLETED (Bob, 2026-09-07)

### Što je urađeno (`frontend/dashboard.html`)

**Sidenav** (L836–840):
- Novi nav item `data-view="detekcija"` s ikonom 🛡 i labelom "Detekcija", umetnut iza Automatizacija

**VIEW: Detekcija** (L1121–1159):
- Topnav s naslovom "Detekcija emailova", subtitleom o audit logu, i "Osvježi" dugmetom
- `v-card` s headerom, toast div `#det-toast`, i `v-table` sa 8 kolona (Vrijeme, Subject, Pošiljalac, Prilozi, Odluka, Stage, Razlog, Akcija)
- tbody `#det-tbody` i count `#det-count`

**JS funkcije** (L2248–2337):
- `loadDetekcija()`: `GET /detection/log?limit=100`; renderuje tabelu s colored pills za odluku, stage badges; requeue dugmad s `data-gmailid` atributom; event listener dodat via `querySelector` (ne inline onclick) — XSS-safe
- `requeueDetection(gmailId)`: `POST /detection/requeue/{encodeURIComponent(gmailId)}`; 409 → "Email je već obrađen"; greška → toast; uspjeh → toast + reload
- `detShowToast(html, type)` / `detHideToast()`: inline toast u ok/warn/error boji
- `_viewActivated` proširen: `if (viewId === 'detekcija') loadDetekcija()`

**XSS disciplina**:
- Sve server vrijednosti (subject, sender, reason, attachment_names, gmail_message_id) kroz `escapeHtml()`
- Gmail ID prenošen isključivo kroz `data-gmailid="${escapeHtml(...)}"` + `btn.dataset.gmailid` — nema raw string interpolacije u JS kontekstu

## Known Gaps
