# PLAN.md — paperless-ai-classifier

> Arbeitsplan zum Weiterbauen. Gedacht als Einstiegspunkt für Claude Code
> oder einen späteren Chat-Turn. Stand: April 2026.

---

## 1. Projekt-Zweck (TL;DR)

KI-Klassifikator für Paperless-NGX. Pollt die Inbox (Tag `Posteingang`), lässt
ein lokales Ollama-Modell fünf Metadaten-Felder vorschlagen, zeigt die
Vorschläge in einer Review-GUI und schreibt sie nach manueller Freigabe via
PATCH zurück in Paperless.

**Fünf Felder, die modifiziert werden:**
Titel · Datum · Korrespondent · Dokumenttyp · Speicherpfad

**Zusätzlich:** Tag-Vorschläge mit Whitelist-Gate (neue Tags nur nach expliziter
Freigabe).

---

## 2. Nicht-Ziele (bewusst ausgeschlossen)

- Kein Re-OCR. Nur der Volltext, den Paperless bereits extrahiert hat.
- Keine Content-Modifikation. Nur Metadaten.
- Keine Auto-Tag-Erstellung ohne Whitelist.
- Keine Multi-User-Auth. Single-Deployment, optional Basic-Auth.
- Keine Vektor-DB außerhalb von SQLite (`sqlite-vec` reicht für die Größenordnung).
- Kein Celery / Redis. APScheduler im selben Prozess genügt.

---

## 3. Architektur

**Ein einzelner Docker-Container** mit drei logischen Komponenten im selben
Python-Prozess:

1. **Worker** — APScheduler-Job, der alle `POLL_INTERVAL_SECONDS` die
   Paperless-Inbox pollt, neue Dokumente an Ollama schickt und Vorschläge in
   SQLite speichert.
2. **Review-GUI** — FastAPI + Jinja2 + HTMX. Kein SPA, kein Build-Schritt.
   Zeigt die Queue, erlaubt Accept/Reject/Edit pro Dokument, verwaltet die
   Tag-Whitelist.
3. **SQLite-State** — persistiert Vorschläge, Embedding-Index (via `sqlite-vec`),
   Tag-Whitelist, Audit-Log, Error-Log.

```
┌────────────────┐   poll     ┌──────────────────────┐
│ Paperless-NGX  │◀───────────│  Worker (APScheduler)│
│  (Tag: Post-   │            │   - fetch inbox docs │
│   eingang)     │──docs─────▶│   - build context    │
└────────────────┘            │   - call Ollama      │
        ▲                     │   - store suggestion │
        │                     └──────────┬───────────┘
        │ PATCH                          │
        │ (nach Freigabe)                ▼
        │                     ┌──────────────────────┐
        │                     │   SQLite + vec0      │
        │                     └──────────┬───────────┘
        │                                │
        │                                ▼
        │                     ┌──────────────────────┐
        └─────────────────────│  FastAPI + HTMX GUI  │
                              └──────────────────────┘
```

---

## 4. Stack

- **Runtime:** Python 3.12, Single-Container
- **Web:** FastAPI, Uvicorn, Jinja2, HTMX (via CDN), Tailwind optional via CDN
- **Scheduler:** APScheduler (AsyncIOScheduler)
- **HTTP:** httpx (async)
- **DB:** SQLite mit `sqlite-vec` Extension
- **LLM-Backend:** Ollama (Default `gemma3:4b`, konfigurierbar)
- **Embedding:** Ollama `nomic-embed-text` (768 dim)
- **Logging:** structlog (JSON in Prod, dev-rendered lokal)
- **Deployment:** GitHub Actions build → GHCR → Dockhand (wie thermomix-bot)
- **Reverse Proxy:** Zoraxy (intern, keine offenen Ports)

---

## 5. Aktueller Build-Stand (bereits vorhanden)

Stand nach letztem Chat-Turn. Diese Dateien sind fertig und können 1:1
übernommen werden:

### Root-Ebene

- [x] `README.md` — vollständig mit Features, Architektur, Quickstart, ENV-Tabelle
- [x] `CLAUDE.md` — Projekt-Kontext für Claude Code
- [x] `pyproject.toml` — Python 3.12, alle Dependencies
- [x] `Dockerfile` — Python 3.12-slim, tini, Healthcheck, `/data` Volume
- [x] `docker-compose.yml` — Single-Service, Dockhand-ready
- [x] `.env.example` — alle Variablen dokumentiert
- [x] `.gitignore`

### app/

- [x] `app/__init__.py`
- [x] `app/config.py` — `pydantic-settings`, `Settings`-Singleton
- [x] `app/db.py` — komplettes SQLite-Schema, `sqlite-vec` Setup, `init_db()`, `get_conn()` Context-Manager. Schema enthält: `processed_documents`, `suggestions`, `tag_whitelist`, `errors`, `doc_embeddings` (vec0), `doc_embedding_meta`, `audit_log`
- [x] `app/models.py` — Pydantic-Modelle für `PaperlessDocument`, `PaperlessEntity`, `ClassificationResult`, `ProposedTag`, `SuggestionRow`, `TagWhitelistEntry`, `ReviewDecision`

### app/clients/

- [x] `app/clients/__init__.py`
- [x] `app/clients/paperless.py` — vollständiger async Client: `ping`, `list_inbox_documents`, `get_document`, `patch_document`, `list_all_documents` (für Initial-Index), `list_correspondents`, `list_document_types`, `list_tags`, `list_storage_paths`, `create_tag`. Pagination sauber implementiert.
- [x] `app/clients/ollama.py` — `ping`, `model_available`, `chat_json` (format=json), `embed`

### app/pipeline/

- [x] `app/pipeline/__init__.py`
- [x] `app/pipeline/context_builder.py` — `_serialize_embedding`, `index_document`, `find_similar_documents` (via `sqlite-vec` MATCH + distance sort)
- [x] `app/pipeline/classifier.py` — `build_user_prompt`, `classify` (lädt System-Prompt aus `prompts/`, ruft Ollama, parsed `ClassificationResult`)

---

## 6. Offene Aufgaben (in empfohlener Reihenfolge)

### 6.1 Prompts (zuerst — blockiert `classifier.py` beim ersten Lauf)

**`prompts/classify_system.txt`** — deutscher System-Prompt. Muss enthalten:

- Rolle: „Du bist ein Klassifikator für gescannte Dokumente in Paperless-NGX."
- Aufgabe: fünf Felder + Tag-Vorschläge liefern
- Strikte Regeln:
  - Datum im Format `YYYY-MM-DD` oder `null`
  - Nur Korrespondenten/Doctypes/Speicherpfade vorschlagen, die in der Liste vorkommen, ODER neue klar als neu kennzeichnen (Name exakt wie im Dokument)
  - Titel kurz, prägnant, ohne Datum (Paperless macht das Datum separat)
  - Tags: bevorzugt aus der vorhandenen Liste, neue nur wenn wirklich neu
  - Konfidenz-Score ehrlich einschätzen (0–100)
  - Antwort ausschließlich als JSON, kein Markdown, keine Erklärung außerhalb des `reasoning`-Feldes
- Beispiel-Output (few-shot, 1–2 Beispiele)

**`prompts/ocr_correction_system.txt`** — optional, nur wenn `ENABLE_OCR_CORRECTION=true`:

- Rolle: „Du korrigierst offensichtliche OCR-Fehler in deutschem Text."
- Regeln: nur eindeutige Fehler (l↔1, O↔0, rn↔m, zerbrochene Wörter), keine inhaltlichen Änderungen, Zeilenumbrüche beibehalten
- Output: `{"corrected_text": "...", "num_corrections": N}`

### 6.2 `app/pipeline/committer.py`

Schreibt angenommene Vorschläge zurück in Paperless.

**Signatur:**
```python
async def commit_suggestion(
    suggestion: SuggestionRow,
    decision: ReviewDecision,
    paperless: PaperlessClient,
) -> None
```

**Verantwortung:**
1. Felder-Dict für PATCH zusammenbauen: `title`, `created_date`, `correspondent`, `document_type`, `storage_path`, `tags`
2. Tag-Liste mergen: aktuelle Tags des Dokuments MINUS `PAPERLESS_INBOX_TAG_ID` PLUS `decision.tag_ids` PLUS optional `PAPERLESS_PROCESSED_TAG_ID`
3. `paperless.patch_document()` aufrufen
4. `suggestions.status = 'committed'` setzen
5. `processed_documents.status = 'committed'` setzen
6. Audit-Log-Eintrag schreiben
7. Bei Fehler: `errors`-Tabelle + `status='error'`, kein Re-Raise nach oben (Worker soll weiterlaufen)

**Invariante:** Nur Tag-IDs verwenden, die in `tag_whitelist` mit `approved=1` stehen ODER bereits vorher in Paperless existierten. Niemals direkt neue Tags erzeugen — das macht der Tag-Approval-Flow separat.

### 6.3 `app/pipeline/ocr_correction.py`

Optional-Pass, nur wenn `settings.enable_ocr_correction=true`.

**Signatur:**
```python
async def maybe_correct_ocr(
    doc: PaperlessDocument,
    ollama: OllamaClient,
) -> tuple[str, int]  # (corrected_text, num_corrections)
```

Heuristik für „OCR kaputt": viele `?`, viele einbuchstabige Wörter, viele
Nicht-ASCII-Artefakte. Wenn Heuristik negativ → Original zurückgeben.
Korrekturen landen nicht zurück in Paperless, sondern nur als Input für den
Klassifikator (der korrigierte Text wird temporär genutzt).

### 6.4 `app/worker.py`

APScheduler-basierter Hintergrund-Job.

**Aufgaben:**
- `poll_inbox()` Coroutine: holt Inbox-Dokumente, filtert gegen `processed_documents` (idempotent via `last_updated_at`), ruft die Pipeline für neue Docs
- Pipeline pro Dokument:
  1. `processed_documents` markieren als `pending`
  2. (optional) OCR-Correction
  3. `context_builder.find_similar_documents()`
  4. Entitäten-Listen cachen (1× pro poll-Zyklus, nicht pro Dokument)
  5. `classifier.classify()`
  6. Ergebnis in `suggestions` schreiben, IDs aus Namen resolven (fuzzy match gegen Entitäten-Listen)
  7. Wenn `auto_commit_confidence > 0` und Score darüber → sofort committen
  8. Danach: Dokument embedden und in `doc_embeddings` indexieren (für zukünftigen Kontext)
- Fehler pro Dokument: in `errors` schreiben, nächstes Dokument
- `start_scheduler(app)` / `stop_scheduler(app)` für Lifespan

**Wichtig:** `AsyncIOScheduler`, kein `BackgroundScheduler` — wir sind in einer
async Welt.

### 6.5 `app/indexer.py`

Initial- und Reindex-Jobs.

**Funktionen:**
- `async def initial_index(paperless, ollama, limit=None)` — läuft beim ersten Start (oder per Button in der GUI), holt alle bereits klassifizierten Dokumente (NICHT die Inbox), embedded sie und füllt `doc_embeddings`. Skip-Logik: Dokumente die bereits in `doc_embedding_meta` stehen werden übersprungen.
- `async def reindex_all(paperless, ollama)` — löscht `doc_embeddings` + `doc_embedding_meta`, dann neu. Für den Fall dass das Embedding-Modell gewechselt wird.

### 6.6 `app/main.py`

FastAPI Entry-Point.

**Muss enthalten:**
- Logging-Setup (structlog)
- `lifespan` Context-Manager:
  - `init_db()`
  - Clients initialisieren (`PaperlessClient`, `OllamaClient`) und in `app.state` ablegen
  - Healthchecks gegen Paperless + Ollama (nur Warnung, kein Fail — Service soll auch starten wenn Backends temporär weg sind)
  - Scheduler starten
  - Shutdown: Scheduler stoppen, Clients schließen
- Router einhängen: `index`, `review`, `tags`, `ocr`, `errors`, `stats`, `settings`, `webhook`
- `/healthz` — simpler JSON-Check
- `/` → Redirect auf `/review` oder Dashboard
- Static Files mounten (`/static`)
- Optional Basic Auth Middleware wenn `GUI_USERNAME` + `GUI_PASSWORD` gesetzt

### 6.7 Routes

Alle unter `app/routes/`, jeweils mit eigenem `APIRouter`.

#### `routes/__init__.py`
Leer.

#### `routes/index.py`
- `GET /` — Dashboard mit Zählern (pending, committed heute, errors, whitelist-pending)
- Template: `index.html`

#### `routes/review.py`
- `GET /review` — Liste aller `suggestions` mit `status='pending'`, neueste zuerst
- `GET /review/{id}` — Detail-View eines Vorschlags, zeigt Original vs. Vorschlag nebeneinander
- `POST /review/{id}/accept` — HTMX endpoint, empfängt Formular, ruft `committer.commit_suggestion()`, gibt HTMX-Partial zurück (Zeile aus Liste entfernen oder Erfolgsmeldung)
- `POST /review/{id}/reject` — Markiert als rejected, Audit-Log
- `POST /review/{id}/edit` — Speichert editierte Felder in `suggestions` (aber ohne commit)
- Templates: `review.html` (Liste), `review_detail.html` (Detail)

#### `routes/tags.py`
- `GET /tags` — Tabelle aller Einträge in `tag_whitelist` mit Status
- `POST /tags/{name}/approve` — Setzt `approved=1`, legt Tag in Paperless an via `paperless.create_tag()`, speichert `paperless_id`
- `POST /tags/{name}/reject` — Löscht aus Whitelist
- Template: `tags.html`

#### `routes/ocr.py`
- Nur wenn `ENABLE_OCR_CORRECTION=true`
- `GET /ocr` — Liste von Dokumenten mit OCR-Korrektur-Vorschlägen (aus separater Tabelle, nur wenn wir das ausbauen — erstmal Platzhalter)
- Template: `ocr.html` (kann MVP „Coming soon" sein)

#### `routes/errors.py`
- `GET /errors` — Letzte 100 Einträge aus `errors`, gruppiert nach `stage`
- `POST /errors/{id}/retry` — Setzt `processed_documents.status` zurück auf `NULL` für das Dokument, damit der nächste Poll-Zyklus es neu versucht
- Template: `errors.html`

#### `routes/stats.py`
- `GET /stats` — Counts pro Status, letzte 7 Tage, evtl. einfacher Chart mit Chart.js via CDN
- Template: `stats.html`

#### `routes/settings.py`
- `GET /settings` — Read-only Dump der aktuellen Config (Token maskiert!)
- `POST /settings/trigger-poll` — Manueller Trigger des Worker-Jobs
- `POST /settings/trigger-reindex` — Manueller Trigger `indexer.reindex_all()`
- Template: `settings.html`

#### `routes/webhook.py`
- `POST /webhook/paperless` — Optional, für den Fall dass man einen Post-Consume-Script-Hook in Paperless einrichtet. Empfängt `document_id`, triggert die Pipeline sofort für dieses eine Dokument.
- Kein Template, nur JSON-Response.

### 6.8 Templates

Jinja2 + HTMX via CDN (`https://unpkg.com/htmx.org@2.0.3`). Tailwind via CDN
für schnelles Styling (`https://cdn.tailwindcss.com`).

- `templates/base.html` — Layout mit Nav (Dashboard, Review, Tags, OCR, Errors, Stats, Settings), Footer, HTMX + Tailwind Script-Tags
- `templates/index.html` — Dashboard-Karten
- `templates/review.html` — Tabelle mit pending suggestions
- `templates/review_detail.html` — Side-by-side Original vs. Vorschlag, Formular mit Dropdowns für Korrespondent/Doctype/Speicherpfad/Tags (pre-selected mit Vorschlag, aber editierbar), Accept/Reject Buttons
- `templates/tags.html` — Whitelist-Tabelle
- `templates/ocr.html` — Platzhalter
- `templates/errors.html` — Fehlerliste mit Retry-Buttons
- `templates/stats.html` — Counter + Chart
- `templates/settings.html` — Config-Dump + Trigger-Buttons
- `templates/partials/suggestion_row.html` — HTMX-swappable row für Review-Liste

### 6.9 Static

- `static/app.css` — minimal, ergänzt Tailwind (ein paar Custom-Utilities)
- Ggf. `static/favicon.ico`

### 6.10 GitHub Actions

**`.github/workflows/docker-publish.yml`:**

- Trigger: Push auf `main`, Tags `v*`
- Jobs: Checkout → Docker Buildx → Login gegen `ghcr.io` → Build + Push
  mit Tags `:latest`, `:sha-xxx`, `:vX.Y.Z`
- Analog zum thermomix-bot Workflow
- Braucht `packages: write` Permission und `GITHUB_TOKEN`

### 6.11 Sonstiges

- [ ] `LICENSE` — MIT, Name Paul Friedrich
- [ ] `tests/` — minimaler Smoke-Test für `db.init_db()` und `models.ClassificationResult` Parsing (optional, kann MVP weglassen)

---

## 7. Wichtige Design-Entscheidungen (nicht ändern ohne Grund)

1. **Idempotenz über `last_updated_at`:** Ein Dokument wird pro Paperless-Update nur einmal verarbeitet. Wenn Paul es in Paperless manuell anfasst, bekommt es einen neuen Timestamp → wird neu verprobt.
2. **Tag-Whitelist-Gate:** Das LLM darf Tag-Namen vorschlagen, aber `committer.py` darf nur IDs verwenden, die bereits in Paperless existieren. Neue Tag-Namen landen in `tag_proposals` (siehe Schema in `db.py`: Tabelle `tag_whitelist`). Erst nach `/tags/.../approve` wird der Tag in Paperless angelegt.
3. **Confidence-Gate:** `AUTO_COMMIT_CONFIDENCE=0` ist der sichere Default. Auto-Commit nur wenn Paul es explizit aktiviert.
4. **Single-Container:** Keine Service-Trennung. Worker und GUI im selben Prozess, shared SQLite. Reduziert Deploy-Komplexität dramatisch.
5. **Read-Only bei Backend-Ausfall:** Wenn Paperless oder Ollama weg sind, wird ein Error-Record geschrieben, der Worker läuft weiter, die GUI zeigt die bisherigen Suggestions weiter an. Kein Crash-Loop.
6. **Embedding-Index baut sich organisch:** Beim ersten Start ist der Index leer. Jedes klassifizierte Dokument wird nach dem Commit indexiert. Initial-Index ist ein manuell triggerbarer Einmal-Job über `/settings/trigger-reindex`.
7. **Kein Celery, kein Redis:** APScheduler im selben Prozess genügt. Das Volumen ist < 100 Dokumente/Tag.
8. **`sqlite-vec` statt `sqlite-vss` oder FAISS:** Einziges Embedding-Store das ohne externe Prozesse und als pip-Wheel läuft.

---

## 8. Deployment-Workflow (Ziel)

Identisch zum `thermomix-bot`:

1. Privates GitHub-Repo `pfriedrich84/paperless-ai-classifier`
2. `git push` → GitHub Actions baut Image → GHCR
3. Dockhand: Git-Stack zeigt auf das Repo, nutzt `docker-compose.yml`
4. `.env` liegt auf dem Host unter `/opt/stacks/paperless-ai-classifier/.env`
5. Dockhand Auto-Sync (oder Webhook) zieht neue Compose-Versionen
6. Zoraxy-Route auf `http://docker-host:8088` legt die GUI intern erreichbar

---

## 9. Wie mit Claude Code weitermachen

Claude Code im leeren Repo starten:

```bash
cd paperless-ai-classifier
claude
```

Erster Prompt an Claude Code:

> Lies `CLAUDE.md` und `PLAN.md`. Dann setze Abschnitt 6 aus `PLAN.md`
> systematisch um, in der dort angegebenen Reihenfolge (6.1 zuerst, dann
> 6.2 …). Nutze die bereits vorhandenen Dateien unter `app/` als
> Referenz für Stil und Patterns. Committe nach jedem Abschnitt separat.

Claude Code hat damit alles, was es braucht:
- `CLAUDE.md` für den Projekt-Kontext
- `PLAN.md` für die konkrete Aufgabenliste
- Die bereits gebauten Dateien als Stil-Referenz (httpx-async, structlog, Pydantic v2)

---

## 10. Offene Fragen / bewusst liegen gelassen

- **Embedding-Dimension hardcoded auf 768 (`nomic-embed-text`).** Wenn das Modell wechselt → `EMBED_DIM` in `db.py` anpassen UND reindex triggern. Könnte man dynamisch machen, ist mir für MVP zu viel Komplexität.
- **Fuzzy-Matching Entitäten-Name → ID:** Im Worker. Einfachster Ansatz: exakter Name-Match case-insensitive, sonst „neu". `rapidfuzz` als optionale Dep wenn das nicht reicht.
- **Bulk-Approve in Review-GUI:** Nice-to-have, nicht MVP.
- **Prometheus-Metrics:** Nice-to-have, nicht MVP.
- **Paperless Post-Consume Hook für Push statt Poll:** Braucht ein Shell-Script im Paperless-Container das `/webhook/paperless` aufruft. Kann später.
- **`gemma4:e4b` wie ursprünglich genannt existiert nicht.** Als Default `gemma3:4b` gesetzt. Wenn du ein anderes Modell willst → `OLLAMA_MODEL` in `.env`.

---

## 11. Checkliste für den ersten produktiven Lauf

Nach vollständigem Build, vor dem ersten echten Einsatz:

- [ ] Paperless-NGX erreichbar, API-Token erstellt
- [ ] Inbox-Tag in Paperless angelegt, ID ermittelt (`curl .../api/tags/ | jq`)
- [ ] Ollama läuft, `gemma3:4b` gepullt
- [ ] Ollama läuft, `nomic-embed-text` gepullt
- [ ] `.env` auf dem Host ausgefüllt
- [ ] `docker compose up -d --build` läuft durch, Container wird `healthy`
- [ ] GUI auf Port 8088 erreichbar
- [ ] `/settings/trigger-reindex` manuell ausführen (Initial-Embedding-Index)
- [ ] Testdokument mit Tag `Posteingang` in Paperless hochladen
- [ ] Nach ≤ `POLL_INTERVAL_SECONDS` erscheint der Vorschlag in `/review`
- [ ] Accept durchklicken, prüfen dass PATCH in Paperless ankommt
- [ ] Error-Log prüfen
- [ ] `AUTO_COMMIT_CONFIDENCE` erst erhöhen wenn 20–30 manuelle Reviews zeigen dass das Modell zuverlässig ist
