# CLAUDE.md — paperless-ai-classifier

Kontext fuer Claude / Claude Code, wenn an diesem Repo gearbeitet wird.

## Projekt-Zweck

KI-basierter Klassifikator fuer Paperless-NGX. Pollt die Inbox (Tag `Posteingang`), laesst ein lokales Ollama-Modell fuenf Felder vorschlagen (Titel, Datum, Korrespondent, Dokumenttyp, Speicherpfad), zeigt die Vorschlaege in einer Review-GUI, und schreibt sie nach manueller Freigabe via PATCH zurueck in Paperless.

## Nicht-Ziele

- **Kein Re-OCR** der Dokumente. Wir nutzen nur den Volltext, den Paperless bereits extrahiert hat.
- **Keine Content-Modifikation.** Wir aendern nur Metadaten.
- **Keine Auto-Tag-Erstellung.** Neue Tags, die das LLM vorschlaegt, muessen explizit in der Whitelist freigegeben werden.
- **Keine Multi-User-Auth.** Single-Deployment, optional Basic-Auth-Schutz.

## Stack

- Python 3.12, FastAPI + Uvicorn
- HTMX + Jinja2 fuer das Review-Frontend (kein SPA, kein Build-Schritt)
- SQLite mit sqlite-vec fuer Embedding-Similarity-Kontext
- APScheduler fuer den Worker-Loop
- httpx fuer alle HTTP-Calls (Paperless, Ollama)
- structlog fuer strukturierte Logs
- Single Docker Container, Dockhand-kompatibel

## Projektstruktur

```
app/
  main.py              FastAPI-App, Lifespan startet Worker + Telegram + DB-Init
  config.py            pydantic-settings, alles aus .env
  db.py                SQLite-Setup, Schema-Migration, sqlite-vec laden
  models.py            Pydantic-Modelle (Suggestion, TagProposal, etc.)
  worker.py            APScheduler-Job: poll_inbox() periodisch
  indexer.py           Initialer + inkrementeller Reindex der Embeddings
  telegram_handler.py  Telegram-Benachrichtigungen + Inline-Keyboard-Callbacks
  clients/
    paperless.py       Paperless-NGX API Client
    ollama.py          Ollama Chat + Embedding Client
    telegram.py        Telegram Bot API Client (httpx, Long-Polling)
  pipeline/
    ocr_correction.py  Optional: OCR-Fehler via LLM korrigieren
    context_builder.py Aehnliche Dokumente via Embedding-Similarity finden
    classifier.py      Prompt bauen, Ollama aufrufen, JSON parsen
    committer.py       Angenommene Vorschlaege nach Paperless schreiben
  mcp_server.py        MCP Server Entrypoint (FastMCP, Lifespan)
  mcp_tools/
    _deps.py           Deps-Dataclass (PaperlessClient + OllamaClient)
    _auth.py           API-Key-Pruefung + Rate-Limiter
    documents.py       Dokument-Tools (search, get, list_inbox, update)
    entities.py        Entity-Tools (correspondents, doctypes, tags, storage_paths)
    classify.py        KI-Tools (classify_document, find_similar)
    suggestions.py     Suggestion-Tools (list, get, approve, reject)
    tags.py            Tag-Whitelist-Tools (list_proposals, approve)
    system.py          Status/Health-Tool
    resources.py       MCP Resources (inbox, pending suggestions)
  routes/
    index.py           Dashboard / Startseite
    review.py          Review-Queue + Detail + Annehmen/Ablehnen
    tags.py            Tag-Whitelist-Management
    ocr.py             OCR-Korrektur-Vorschlaege (optional)
    errors.py          Fehlerliste + Retry
    stats.py           Counters, Graphen
    settings.py        Read-only View auf Config, Trigger fuer manuellen Run
    webhook.py         Optional: Webhook-Endpoint fuer Paperless-Trigger
  templates/           Jinja2 + HTMX
  static/              CSS
prompts/
  classify_system.txt  System-Prompt fuer Klassifikation (Deutsch)
  ocr_correction_system.txt  System-Prompt fuer OCR-Correction
```

## Paperless-API-Reference (nur was wir brauchen)

- `GET /api/documents/?tags__id__all=<inbox_id>` → Dokumente in Inbox
- `GET /api/documents/<id>/` → Volltext in `content`
- `GET /api/correspondents/` → Liste fuer Whitelist-Matching
- `GET /api/document_types/` → Liste fuer Whitelist-Matching
- `GET /api/tags/` → Liste fuer Whitelist-Matching
- `GET /api/storage_paths/` → Liste
- `PATCH /api/documents/<id>/` → Metadaten-Update
- `POST /api/tags/` → Nur nach expliziter Whitelist-Freigabe

Alle Requests: `Authorization: Token <PAPERLESS_TOKEN>`

## Ollama-Reference

- `POST /api/chat` mit `format: "json"` → strukturierte JSON-Antwort
- `POST /api/embeddings` → Vektor fuer Similarity-Suche
- `GET /api/tags` → Healthcheck + Modell-Liste

## Wichtige Invarianten

1. **Idempotenz:** Ein Dokument wird pro `updated_at`-Timestamp nur einmal verarbeitet. `processed_documents`-Tabelle haelt State.
2. **Tag-Whitelist-Gate:** `tags`-Updates in Paperless passieren NUR mit IDs, die in der Whitelist stehen. Neue vom LLM vorgeschlagene Tags landen in `tag_proposals` mit Status `pending`.
3. **Confidence-Gate:** Nur wenn `AUTO_COMMIT_CONFIDENCE > 0` UND das LLM einen Score darueber meldet wird ohne Review committed.
4. **Read-Only bei Fehler:** Wenn Paperless oder Ollama nicht erreichbar sind, wird ein Error-Record geschrieben und der Worker macht weiter. Keine Retries im selben Lauf.

## Deployment (Dockhand)

Identisch zum `thermomix-bot`-Workflow:

1. Privates GitHub-Repo `pfriedrich84/paperless-ai-classifier`
2. SSH Deploy Key in GitHub hinterlegen (read-only)
3. Dockhand → Settings → Git → Repo hinzufuegen
4. `/opt/stacks/paperless-ai-classifier/.env` manuell auf dem Host anlegen
5. Dockhand → Stacks → Create from Git → Compose Path `docker-compose.yml`
6. External env file: `/opt/stacks/paperless-ai-classifier/.env`
7. Auto-Sync oder Webhook aktivieren

Reverse Proxy: Zoraxy (kein Traefik). Keine Ports gegen Internet.

## Dev-Setup

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
# Werte eintragen
uvicorn app.main:app --reload --port 8088
```

## Telegram-Bot (optional)

Wenn `ENABLE_TELEGRAM=true` und `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` gesetzt:
- Neue Vorschlaege werden als Telegram-Nachricht mit Inline-Keyboard gesendet (Accept / Reject / Edit in GUI)
- Accept/Reject direkt im Chat moeglich, ohne GUI
- Benachrichtigungen werden nur fuer manuell zu reviewende Vorschlaege gesendet (nicht fuer auto-committed)
- Long-Polling (kein Webhook noetig, laeuft hinter NAT/Firewall)

## MCP Server (optional)

Model Context Protocol Server fuer KI-Assistenten (Claude Code, etc.).

```bash
# stdio (fuer Claude Code / lokale Nutzung)
python -m app.mcp_server

# SSE (fuer HTTP-Clients)
MCP_TRANSPORT=sse MCP_PORT=3001 python -m app.mcp_server
```

**Sicherheitskonzept:**
- Read-Only als Default. Write-Tools nur bei `MCP_ENABLE_WRITE=true`.
- `MCP_API_KEY` fuer Auth (empfohlen bei SSE-Transport).
- `MCP_CLASSIFY_RATE_LIMIT=10` begrenzt KI-Klassifikationen pro Stunde.
- `classify_document` akzeptiert nur Dokumente mit Inbox-Tag.

**Tools (read-only, immer verfuegbar):**
- `search_documents`, `get_document`, `list_inbox`
- `list_correspondents`, `list_document_types`, `list_tags`, `list_storage_paths`
- `list_suggestions`, `get_suggestion`
- `list_tag_proposals`
- `classify_document` (rate-limited), `find_similar_documents`
- `get_status`

**Tools (write, opt-in via MCP_ENABLE_WRITE=true):**
- `update_document`, `approve_suggestion`, `reject_suggestion`, `approve_tag`

**Resources:**
- `paperless://suggestions/pending` — Offene Vorschlaege
- `paperless://stats` — Classifier-Statistiken (Suggestions, Errors, Tags)

## Bekannte TODOs / Ausbau

- [ ] Echter sqlite-vec KNN-Index statt Full-Scan (ab ein paar tausend Dokumenten relevant)
- [ ] Re-Embedding-Job wenn das Embedding-Modell wechselt
- [ ] Metrics-Endpoint (Prometheus)
- [ ] Bulk-Approve in der Review-GUI
- [ ] Webhook-Trigger von Paperless statt Polling (Paperless unterstuetzt das nicht nativ, braeuchte ein Custom-Consumer-Hook)
