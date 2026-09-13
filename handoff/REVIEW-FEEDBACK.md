# Review Feedback — Detection recall — Step 3

Reviewer: Richard
Date: 2026-09-07

---

## Must Fix

### 1. `escapeHtml` does not escape `"` — unsafe in attribute context

`escapeHtml` (L1974-1977) replaces only `&`, `<`, `>`. It is used to populate the `data-gmailid` HTML attribute:

```js
data-gmailid="${escapeHtml(row.gmail_message_id || '')}"
```

A Gmail message ID containing a double-quote (`"`) would break out of the attribute and allow attribute injection. Gmail IDs are alphanumeric in practice, but `escapeHtml` must be correct for all inputs it is applied to.

**Fix:** Add `"` → `&quot;` and `'` → `&#039;` to `escapeHtml`. This is a project-wide function — the fix benefits every attribute-context usage across the file.

```js
function escapeHtml(s) {
  const str = s == null ? '' : String(s);
  return str
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#039;');
}
```

---

## Should Fix

### 2. `detShowToast` accepts raw HTML into `innerHTML` — fragile contract

The function signature is `detShowToast(html, type)` and it does `el.innerHTML = html`. The name signals that callers must pre-escape, but nothing enforces this. Current callers are clean (two pass `escapeHtml(...)`, one passes a literal). A future caller that forgets will introduce XSS silently.

**Fix:** Switch to `el.textContent = html` (renaming the parameter to `text`). No caller needs actual markup in the toast — all messages are plain strings.

### 3. `loadDetekcija` fires on every nav click — no lazy-load guard

Consistent with how other views work, so not a regression. But repeated nav clicks cause repeated `/detection/log?limit=100` fetches with the table flickering through "Učitavam…" each time.

**Fix:** Add a module-level flag `let _detLoaded = false;` set after first successful render; skip the fetch in `_viewActivated` if already loaded. The Osvježi button always bypasses the guard. Low-effort, no UX regression.

---

## Escalate

None.

---

## Cleared

- **XSS — subject, sender, reason, ts:** All through `escapeHtml()` before `innerHTML`. Correct.
- **XSS — attachment_names:** Joined then escaped; integer suffix is safe. Correct.
- **XSS — gmail_message_id in URL:** `encodeURIComponent(gmailId)` in `requeueDetection`. Correct. (Must Fix above is the attribute context, not the URL.)
- **XSS — error message in error row:** `escapeHtml(e.message)` on L2298. Correct.
- **Decision pill completeness:** All four decisions mapped; fallback arm uses `escapeHtml(row.decision || '—')`. No `undefined` leak.
- **Stage badge:** `llm` → "AI", everything else → static "pravilo". No `undefined` leak.
- **Requeue button scope:** `canRequeue` limits button to `rejected` and `duplicate` only. Correct.
- **409 handling:** Explicit branch, returns early with Bosnian message. Correct.
- **Other error handling:** `d.detail || HTTP ${r.status}` fallback, surfaced via toast. Correct.
- **Duplicate listeners on re-render:** `tbody.innerHTML = ...` discards old DOM; listeners wired to fresh elements only. No accumulation.
- **Empty state:** "Nema zabilježenih odluka" row rendered. Correct.
- **Fetch failure:** Caught, error row rendered with escaped message. Does not throw. Correct.
- **`encodeURIComponent` on requeue URL:** Present. Correct.
- **Drift:** No other views touched. No new CSS tokens. Reuses `v-btn`, `v-btn-ghost`, `v-table`, `v-card`, `topnav-btn`, existing CSS variables.
- **Sidenav position:** After Automatizacija, before Postavke. Correct per spec.

---

## Ready for Builder: NO

Must Fix 1 (`escapeHtml` missing quote escaping) blocks YES. Should Fix 2 is low-effort and should go in the same pass.
