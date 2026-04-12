# CLAUDE.md — paperless-ai-classifier

Kontext fuer Claude / Claude Code, wenn an diesem Repo gearbeitet wird.

## Projekt-Zweck

KI-basierter Klassifikator fuer Paperless-NGX. Pollt die Inbox (Tag `Posteingang`), laesst ein lokales Ollama-Modell fuenf Felder vorschlagen (Titel, Datum, Korrespondent, Dokumenttyp, Speicherpfad), zeigt die Vorschlaege in einer Review-GUI, und schreibt sie nach manueller Freigabe via PATCH zurueck in Paperless. Der `Posteingang`-Tag bleibt nach Commit standardmaessig erhalten (`KEEP_INBOX_TAG=true`).

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
entrypoint.sh            Startet Uvicorn + optional MCP-Server (ENABLE_MCP=true)
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
- `POST /api/embeddings` → Vektor fuer Similarity-Suche (Default: `nomic-embed-text-v2-moe`, multilingual DE/EN). Bei Context-Length-Fehlern (500) wird der Text progressiv um 25% gekuerzt und erneut gesendet. Transiente 5xx/429-Fehler werden mit exponentiellem Backoff wiederholt. Konfigurierbar via `OLLAMA_EMBED_RETRIES` (Default: 3) und `OLLAMA_EMBED_RETRY_BASE_DELAY` (Default: 1.0s).
- `GET /api/tags` → Healthcheck + Modell-Liste

## Wichtige Invarianten

1. **Idempotenz:** Ein Dokument wird pro `updated_at`-Timestamp nur einmal verarbeitet. `processed_documents`-Tabelle haelt State.
2. **Tag-Whitelist-Gate:** `tags`-Updates in Paperless passieren NUR mit IDs, die in der Whitelist stehen. Neue vom LLM vorgeschlagene Tags landen in `tag_proposals` mit Status `pending`.
3. **Confidence-Gate:** Nur wenn `AUTO_COMMIT_CONFIDENCE > 0` UND das LLM einen Score darueber meldet wird ohne Review committed.
4. **Read-Only bei Fehler:** Wenn Paperless oder Ollama nicht erreichbar sind, wird ein Error-Record geschrieben und der Worker macht weiter. Keine Pipeline-Level-Retries im selben Lauf. Ausnahme: `OllamaClient.embed()` hat HTTP-Level-Retries mit Truncation bei Context-Length-Fehlern und Backoff bei transienten 5xx (konfigurierbar via `OLLAMA_EMBED_RETRIES`).
5. **Inbox-Tag bleibt:** Standardmaessig (`KEEP_INBOX_TAG=true`) wird der `Posteingang`-Tag nach Commit NICHT entfernt. Nur mit `KEEP_INBOX_TAG=false` wird er beim Commit entfernt.
6. **Kontext-Qualitaet:** Nur Dokumente die NICHT mehr im Posteingang sind werden als Kontext fuer neue Klassifikationen genutzt. Inbox-Dokumente sind noch nicht reviewed/approved und wuerden unzuverlaessige Metadaten liefern.
7. **Kontext-Anreicherung:** Kontext-Dokumente enthalten ihre vollstaendige Klassifikation (Korrespondent, Dokumenttyp, Speicherpfad, Tags, Datum). Regel 9 im System-Prompt weist das LLM an, diese Metadaten als starke Hinweise zu nutzen.

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
pip install -c constraints.txt -e ".[dev]"
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
Laeuft im selben Container wie die Haupt-App wenn `ENABLE_MCP=true` gesetzt ist.

```bash
# Docker: MCP laeuft im selben Container mit (ENABLE_MCP=true in .env)
docker compose up -d

# Lokal (stdio, fuer Claude Code CLI):
python -m app.mcp_server

# Lokal (SSE):
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

## Supply-Chain-Schutz (Dependency-Pinning)

### Hintergrund: Was ist eine Supply-Chain-Attacke?

Eine Supply-Chain-Attacke (Lieferkettenangriff) zielt nicht direkt auf unseren Code,
sondern auf die Abhaengigkeiten, die wir von externen Quellen (PyPI) beziehen.
Typische Angriffsvektoren:

- **Account-Takeover:** Ein Angreifer uebernimmt das PyPI-Konto eines Maintainers
  und veroeffentlicht eine kompromittierte Version eines beliebten Pakets.
- **Typosquatting:** Ein Paket mit aehnlichem Namen (z.B. `requestes` statt `requests`)
  enthaelt Schadcode.
- **Malicious Update:** Schadcode wird in ein regulaeres Update eingeschleust —
  z.B. ein Cryptominer, Credential-Stealer oder Backdoor.
- **Dependency Confusion:** Ein internes Paket wird durch ein gleichnamiges
  oeffentliches Paket mit hoeherer Versionsnummer ersetzt.

Das Problem: `pip install fastapi>=0.115.0` installiert *immer die neueste Version*.
Wird diese Version Minuten nach der Veroeffentlichung kompromittiert, sind alle
Installationen betroffen — bevor die Community den Angriff bemerkt.

### Unsere Strategie: 14-Tage-Mindestalter

Wir installieren **nur Versionen, die mindestens 14 Tage oeffentlich verfuegbar sind**.
Die Logik dahinter:

1. **Erkennungsfenster:** Die meisten kompromittierten Pakete werden innerhalb von
   Stunden bis wenigen Tagen entdeckt und von PyPI entfernt.
2. **Community-Review:** Nach 14 Tagen haben tausende Entwickler die Version
   installiert und potenzielle Probleme haetten sich gezeigt.
3. **Automatische Erkennung:** Tools wie `pip-audit`, Sicherheitsscanner und
   GitHub-Advisories decken bekannte Schwachstellen typischerweise innerhalb
   einer Woche auf.
4. **Kosten-Nutzen:** 14 Tage Verzoegerung sind fuer eine Self-Hosted-App akzeptabel —
   wir brauchen keine Bleeding-Edge-Features am Erscheinungstag.

### Implementierung

Die Schutzmassnahmen bestehen aus vier Dateien und einem CI-Check:

#### 1. `pyproject.toml` — Direkte Abhaengigkeiten

Jede direkte Abhaengigkeit hat eine **Obergrenze** (`<=`), die auf die letzte
als sicher bekannte Version zeigt:

```toml
dependencies = [
    "fastapi>=0.115.0,<=0.135.2",    # Untergrenze = Mindestfeature, Obergrenze = geprueft
    "uvicorn[standard]>=0.32.0,<=0.42.0",
    ...
]
```

#### 2. `constraints.txt` — Transitive Abhaengigkeiten

Abhaengigkeiten unserer Abhaengigkeiten (z.B. `rich` kommt ueber `mcp → typer → rich`)
werden hier nach oben begrenzt:

```
rich<=14.3.3
click<=8.3.1
cryptography<=46.0.7   # Security-Fix-Ausnahme, siehe unten
...
```

pip wendet Constraints bei jeder Installation an:
`pip install -c constraints.txt -e ".[dev]"`

#### 3. `scripts/check_dependency_age.py` — CI-Pruefung

Dieses Skript fragt fuer jedes installierte Paket die PyPI-API nach dem
Veroeffentlichungsdatum und schlaegt fehl, wenn ein Paket juenger als 14 Tage ist:

```bash
python scripts/check_dependency_age.py --min-days 14
```

#### 4. `.dependency-age-allowlist` — Ausnahmen fuer Security-Patches

Manchmal muss ein Sicherheits-Patch *sofort* eingespielt werden, auch wenn er
weniger als 14 Tage alt ist. Diese Ausnahmen werden hier dokumentiert:

```
# Security fix for CVE-2026-39892 (released 2026-04-08, remove after 2026-04-22)
cryptography==46.0.7
```

**Regeln fuer Ausnahmen:**
- Nur fuer CVE-Fixes von etablierten Paketen.
- Jeder Eintrag muss die CVE-Nummer und das Ablaufdatum enthalten.
- Nach 14 Tagen wird der Eintrag entfernt (das Paket ist dann alt genug).

#### 5. `.pip-audit-known-vulnerabilities` — Bekannte Audit-Ausnahmen

CVEs in Build-Tools (z.B. `pip` selbst), die keine Laufzeit-Abhaengigkeiten sind:

```
CVE-2025-8869
CVE-2026-1703
```

### Dependency-Update-Workflow

Wenn eine neue Version eines Pakets eingespielt werden soll:

1. **Pruefen:** Ist die Version mindestens 14 Tage alt?
   ```bash
   curl -s https://pypi.org/pypi/<paket>/<version>/json | python3 -c "
   import sys,json; print(json.load(sys.stdin)['urls'][0]['upload_time'])"
   ```
2. **Anheben:** Obergrenze in `pyproject.toml` (direkt) oder `constraints.txt`
   (transitiv) auf die neue Version setzen.
3. **Installieren:** `pip install -c constraints.txt -e ".[dev]"`
4. **Testen:** Alle CI-Checks lokal ausfuehren (Lint, Tests, Audit, Age-Check).
5. **Committen:** Aenderung an `pyproject.toml` / `constraints.txt` committen.

**Bei Security-Patches (< 14 Tage alt):**
- Version in `constraints.txt` anheben.
- Eintrag in `.dependency-age-allowlist` mit CVE-Nummer und Ablaufdatum.
- Nach 14 Tagen: Eintrag aus Allowlist entfernen.

### CI-Pipeline-Uebersicht

```
Install (mit constraints.txt)
  → Ruff Lint + Format
  → Tests (pytest)
  → pip check (Kompatibilitaet)
  → pip-audit (bekannte CVEs)
  → Dependency Age Check (14-Tage-Regel)
  → Template-Syntax
  → Import-Check
  → DB-Schema-Check
  → Prompt-File-Check
  → Docker Build
```

## Bekannte TODOs / Ausbau

- [ ] Echter sqlite-vec KNN-Index statt Full-Scan (ab ein paar tausend Dokumenten relevant)
- [ ] Re-Embedding-Job wenn das Embedding-Modell wechselt
- [ ] Metrics-Endpoint (Prometheus)
- [ ] Bulk-Approve in der Review-GUI
- [ ] Webhook-Trigger von Paperless statt Polling (Paperless unterstuetzt das nicht nativ, braeuchte ein Custom-Consumer-Hook)
