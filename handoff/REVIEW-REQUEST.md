# Review Request — Detection recall — Step 3

Ready for Review: YES

## Changed files

### `frontend/dashboard.html`

**Sidenav** (L836–840):
- New `<div class="nav-item" data-view="detekcija">` with 🛡 icon + "Detekcija" label, inserted after Automatizacija nav item

**View HTML** (L1121–1159):
- New `<div id="view-detekcija" class="view">` block
- Topnav: title "Detekcija emailova", subtitle, "↻ Osvježi" button (calls `loadDetekcija()`)
- `#det-count` span, `#det-toast` div
- `v-table` with columns: Vrijeme, Subject, Pošiljalac, Prilozi, Odluka, Stage, Razlog, Akcija
- `tbody#det-tbody` populated by JS

**`_viewActivated` extension** (~L1990):
- Added `if (viewId === 'detekcija') loadDetekcija();` — lazy-loads on first open

**New JS functions** (L2248–2337):
- `loadDetekcija()` — fetches `GET /detection/log?limit=100`, renders table; colored decision pills (accepted=green, rejected=red, duplicate=amber, no_files=grey), stage badges (pravilo/AI), "Vrati u red" buttons for rejected+duplicate rows only; wires buttons via `querySelectorAll('button[data-gmailid]')` + `addEventListener` (no raw id in onclick)
- `requeueDetection(gmailId)` — `POST /detection/requeue/${encodeURIComponent(gmailId)}`; 409 → warn toast; error → error toast; success → ok toast + reload
- `detShowToast(html, type)` / `detHideToast()` — ok/warn/error inline toast in `#det-toast`

## XSS audit
- All server strings (subject, sender, reason, attachment_names, gmail_message_id) go through `escapeHtml()`
- Gmail message ID stored in `data-gmailid="${escapeHtml(...)}"`, retrieved via `btn.dataset.gmailid` — never raw-interpolated into JS or innerHTML
- Requeue URL uses `encodeURIComponent(gmailId)` (variable, not a rendered string)

## What to verify
1. Sidenav "Detekcija" item appears after Automatizacija, expands correctly on hover
2. Clicking Detekcija loads the table from `/detection/log?limit=100`
3. Decision pills: green=Prihvaćen, red=Odbijen, amber=Duplikat, grey=Bez priloga
4. Stage badge: "pravilo" for rule, "AI" for llm
5. "Vrati u red" button present only for rejected/duplicate rows; absent for accepted/no_files
6. "Vrati u red" click calls `POST /detection/requeue/{id}`; 409 shows "Email je već obrađen"
7. "↻ Osvježi" button reloads the table
8. No other views affected
