# Security triage

Tracked work for repository security issues currently scoped by TODO-1b47f416.

## Scope

In scope:
- #162 — lock down `/setup` after first-run
- #163 — sanitize dynamic values in setup/tag UI HTML
- #168 — escape dynamic error values in settings/inbox fragments
- #169 — avoid returning raw exception details in webhook/chat responses
- #32 — add CSRF protection to state-changing endpoints
- #167 — set `Secure` on chat session cookie in HTTPS deployments
- #166 — avoid logging raw webhook request bodies by default
- #3 — older duplicate of #32; close once #32 is fixed

Out of scope:
- #33 multi-user auth / RBAC
- #4 rate limiting
- #114 image-scanner follow-up

## Triage summary

### #162 — `/setup` reconfiguration after first-run
- Severity: High
- Risk: unauthenticated or unintended post-setup configuration changes
- Affected surfaces: `app/main.py`, `app/routes/setup.py`
- Fix strategy: reject setup GET/POST once `needs_setup()` is false and stop exempting `/setup` from Basic Auth middleware.
- Status: fixed locally, pending commit/merge

### #163 / #168 / #169 — unsafe output / raw errors
- Severity: High for #163, Medium for #168 and #169
- Risk: HTML injection / XSS in HTMX fragments and internal detail disclosure in user-visible errors
- Fix strategy: replace string-built HTML where practical, otherwise escape values and return generic errors while logging details server-side.
- Status: fixed locally, pending commit/merge

### #32 / #167 / #3 — request integrity and cookie hardening
- Severity: High for #32, Low for #167
- Risk: authenticated browser can be tricked into cross-site state-changing requests; session cookie missing `Secure` in HTTPS setups
- Fix strategy: add CSRF protection to POST endpoints and set secure cookie behavior based on deployment configuration / request scheme.
- Status: fixed locally, pending commit/merge

### #166 — webhook payload privacy in logs
- Severity: Medium
- Risk: sensitive webhook content leaks into logs
- Fix strategy: log metadata only by default, gate raw previews behind explicit debug flag, redact sensitive fields.
- Status: largely fixed locally via metadata-only logging; keep separate for final verification / issue handling
