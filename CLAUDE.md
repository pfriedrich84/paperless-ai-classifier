# CLAUDE.md — paperless-ai-classifier

Kontext fuer Claude / Claude Code, wenn an diesem Repo gearbeitet wird.

## Projekt-Zweck

KI-basierter Klassifikator fuer Paperless-NGX. Pollt die Inbox (Tag `Posteingang`), laesst ein lokales Ollama-Modell fuenf Felder vorschlagen (Titel, Datum, Korrespondent, Dokumenttyp, Speicherpfad), zeigt die Vorschlaege in einer Review-GUI, und schreibt sie nach manueller Freigabe via PATCH zurueck in Paperless. Der `Posteingang`-Tag bleibt nach Commit standardmaessig erhalten (`KEEP_INBOX_TAG=true`).

## Klassifikationsansatz — Kontext statt Isolation

Viele LLM-basierte Klassifikatoren fuer Dokumentenmanagement verfolgen den gleichen
naiven Ansatz: Dokument-Text an das LLM senden, ein paar verfuegbare Kategorien
auflisten, fertig. Jedes Dokument wird **isoliert** klassifiziert — das Modell sieht
nur den Rohtext und eine Liste von Entitaetsnamen, aber nie, wie fruehere Dokumente
tatsaechlich eingeordnet wurden.

Dieser Klassifikator geht einen fundamentalen Schritt weiter:

### Kontext-basierte Klassifikation (Few-Shot aus eigenen Daten)

```
                  Neues Dokument
                       |
                  [Embedding]          ← nomic-embed-text-v2-moe
                       |
              KNN-Suche in sqlite-vec
                       |
          +-----------+-----------+
          |           |           |
      Dok #312    Dok #891    Dok #45     ← Aehnlichste bereits klassifizierte Dokumente
      Rechnung    Rechnung    Rechnung
      Stadtwerke  Stadtwerke  EnBW
      Finanzen/   Finanzen/   Finanzen/
          |           |           |
          +-----------+-----------+
                       |
               Kontext-Prompt:
               "Diese 3 aehnlichen Dokumente wurden so klassifiziert: ..."
               + Zieldokument
                       |
                  [Klassifikation]     ← gemma4:e2b
                       |
                JSON-Vorschlag mit hoher Konfidenz
```

**Warum ist das besser?**

1. **Implizites Few-Shot-Learning:** Das LLM sieht nicht nur abstrakte Kategorienamen
   (`"Rechnung"`, `"Vertrag"`), sondern konkrete Beispiele aus dem eigenen Archiv.
   Wenn drei aehnliche Dokumente alle dem Korrespondenten "Stadtwerke Muenchen" und
   dem Typ "Rechnung" zugeordnet wurden, ist das ein starkes Signal — staerker als
   jede generische Prompt-Anweisung.

2. **Selbstverbesserung ohne Training:** Mit jedem klassifizierten Dokument waechst
   der Embedding-Index. Neue Dokumente profitieren automatisch von besseren Kontexten.
   Nach 50 klassifizierten Rechnungen desselben Absenders ist die Trefferquote
   praktisch 100% — ohne jedes Fine-Tuning.

3. **Benutzer-Praeferenzen statt Annahmen:** Der Klassifikator lernt die
   *tatsaechliche* Ordnungslogik des Benutzers. Wenn jemand Gasrechnungen unter
   "Nebenkosten" statt "Energie" einsortiert, uebernimmt das System dieses Muster
   aus dem Kontext — ein generischer Prompt wuerde raten.

4. **Kleine Modelle, grosse Ergebnisse:** Durch den reichen Kontext kann ein
   kompaktes Modell wie `gemma4:e2b` Ergebnisse liefern,
   die ohne Kontext ein deutlich groesseres Modell erfordern wuerden. Der Kontext
   kompensiert fehlende Modellkapazitaet.

5. **Robust bei mehrdeutigen Dokumenten:** Ein Brief, der sowohl von der Hausverwaltung
   als auch vom Energieversorger stammen koennte, wird durch den Kontext eindeutig:
   Wenn aehnliche Dokumente mit diesem Sprachstil bisher immer der Hausverwaltung
   zugeordnet wurden, folgt das System diesem Muster.

### Qualitaetsschranken

Der Kontext allein genuegt nicht — zusaetzlich greifen mehrere Sicherheitsnetze:

- **Entity-Whitelisting:** Das LLM darf nur existierende Korrespondenten, Dokumenttypen
  und Speicherpfade vorschlagen. Neue Tags landen in einer Freigabe-Queue.
- **Confidence-Gate:** Nur bei explizit konfigurierter Mindest-Konfidenz wird
  automatisch committed — sonst immer manuelles Review.
- **Inbox-Exclusion:** Noch nicht reviewte Dokumente werden nie als Kontext genutzt,
  um fehlerhafte Klassifikationen nicht zu propagieren.
- **Token-Budget-Management:** Der Prompt verteilt 60% des Kontextfensters auf das
  Zieldokument (max `MAX_DOC_CHARS`, Default 32000) und 40% auf Kontext-Dokumente,
  mit dynamischem Fallback wenn der Platz knapp wird. `EMBED_MAX_CHARS` (Default
  16000) begrenzt den Text fuer Embeddings (Titel + Content gesamt).

## Nicht-Ziele

- **Kein Re-OCR durch Paperless.** Wir nutzen den Volltext, den Paperless bereits extrahiert hat, als Basis. Optional kann ein Vision-LLM die OCR-Qualitaet verbessern, indem es den Text gegen die Originaldokument-Bilder vergleicht (konfigurierbar via `OCR_MODE`). Korrigierter Text wird nur lokal in `doc_ocr_cache` gespeichert, nie zurueck nach Paperless geschrieben.
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
  worker.py            APScheduler-Job: poll_inbox() mit Phasen-Pipeline
  indexer.py           Initialer + inkrementeller Reindex der Embeddings
  telegram_handler.py  Telegram-Benachrichtigungen + Inline-Keyboard-Callbacks + RAG-Chat
  config_writer.py     Persistente Settings: config.env schreiben + hot-reload
  chat.py              RAG-Chat-Core: Session-Management + ask()-Pipeline
  clients/
    paperless.py       Paperless-NGX API Client
    ollama.py          Ollama Chat + Embedding Client
    telegram.py        Telegram Bot API Client (httpx, Long-Polling)
  pipeline/
    ocr_correction.py  Multi-Level OCR-Korrektur (off/text/vision_light/vision_full)
    pdf_renderer.py    PDF/Bild → base64 JPEG fuer Vision-Modelle (PyMuPDF)
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
    tags.py            Tag-Whitelist- und Blacklist-Tools (list_proposals, approve, list_blacklisted, unblacklist)
    system.py          Status/Health-Tool
    resources.py       MCP Resources (inbox, pending suggestions)
  routes/
    index.py           Dashboard / Startseite
    chat.py            RAG-Chat: Fragen zu Dokumenten stellen (/chat)
    review.py          Review-Queue + Detail + Annehmen/Ablehnen
    tags.py            Tag-Whitelist- und Blacklist-Management
    ocr.py             OCR-Korrektur-Vorschlaege (optional)
    errors.py          Fehlerliste + Retry
    stats.py           Counters, Graphen
    settings.py        Config-Editor, Prompt-Editor, Trigger fuer manuellen Run
    webhook.py         Optional: Webhook-Endpoint fuer Paperless-Trigger
    inbox.py           Inbox-Ansicht: Dokumenten-Karten + Bulk-Aktionen
    embeddings.py      Vektor-DB Dashboard + Similarity-Search
    setup.py           Onboarding-Wizard mit Verbindungstests
  templates/           Jinja2 + HTMX
  static/              CSS + lokal gebundelte htmx.min.js
prompts/
  classify_system.txt          System-Prompt fuer Klassifikation (Deutsch)
  ocr_correction_system.txt    System-Prompt fuer Text-Only OCR-Correction
  ocr_vision_light_system.txt  System-Prompt fuer Vision-OCR (Bild + Text vergleichen)
  ocr_vision_full_system.txt   System-Prompt fuer Vision-OCR (Seite-fuer-Seite)
  chat_system.txt              System-Prompt fuer RAG-Chat (Deutsch)
entrypoint.sh            Startet Uvicorn + optional MCP-Server (ENABLE_MCP=true)
docs/
  architecture.md        Gesamtarchitektur + Datenfluss-Diagramme
  webhooks.md            Webhook-Konfigurationsanleitung
tests/                   pytest-Tests (conftest + 14 test_*.py)
scripts/
  check_dependency_age.py  CI-Check: 3-Tage-Mindestalter fuer Dependencies
.github/workflows/
  ci.yml                 Lint, Tests, Audit, Docker Build
  docker-publish.yml     GHCR Image Publish bei Release/Push
.claude/
  settings.json          Permissions (allow/deny) + PreToolUse-Hooks
  commands/
    precommit.md         Ruff-Check + Format + pytest
    dep-update.md        Dependency-Update mit 3-Tage-Supply-Chain-Pruefung
    ci-local.md          Lokale CI-Simulation (alle Checks wie in GitHub Actions)
```

## Paperless-API-Reference (nur was wir brauchen)

- `GET /api/documents/?tags__id__all=<inbox_id>` → Dokumente in Inbox
- `GET /api/documents/<id>/` → Volltext in `content`
- `GET /api/correspondents/` → Liste fuer Whitelist-Matching
- `GET /api/document_types/` → Liste fuer Whitelist-Matching
- `GET /api/tags/` → Liste fuer Whitelist-Matching
- `GET /api/storage_paths/` → Liste
- `GET /api/documents/<id>/download/` → Original-Datei herunterladen (PDF/Bild, fuer Vision-OCR)
- `PATCH /api/documents/<id>/` → Metadaten-Update
- `POST /api/tags/` → Nur nach expliziter Whitelist-Freigabe

Alle Requests: `Authorization: Token <PAPERLESS_TOKEN>`

## Ollama-Reference

- `POST /api/chat` mit `format: "json"` → strukturierte JSON-Antwort. Unterstuetzt auch `images`-Feld fuer Vision-Modelle (base64-encoded, kein Data-URI-Prefix).
- `POST /api/embeddings` → Vektor fuer Similarity-Suche (Default: `nomic-embed-text-v2-moe`, multilingual DE/EN). Bei Context-Length-Fehlern (500) wird der Text progressiv um 25% gekuerzt und erneut gesendet. Transiente 5xx/429-Fehler werden mit exponentiellem Backoff wiederholt. Konfigurierbar via `OLLAMA_EMBED_RETRIES` (Default: 3) und `OLLAMA_EMBED_RETRY_BASE_DELAY` (Default: 1.0s).
- `POST /api/generate` mit `keep_alive: 0` → Modell aus VRAM entladen (genutzt zwischen Pipeline-Phasen)
- `GET /api/tags` → Healthcheck + Modell-Liste

**Drei Modelle im Einsatz:**

| Modell | Zweck | Konfiguration | Context Window |
|--------|-------|---------------|----------------|
| `nomic-embed-text-v2-moe` | Embedding-Similarity-Suche | `OLLAMA_EMBED_MODEL` | 8192 Tokens (`OLLAMA_EMBED_NUM_CTX`) |
| `gemma4:e2b` | Klassifikation + Vision-OCR (Titel, Datum, etc.) | `OLLAMA_MODEL` | 8192 Tokens (`OLLAMA_NUM_CTX`) |
| `gemma3:1b` | Text-Only OCR-Korrektur (optional, kleiner/schneller) | `OLLAMA_OCR_MODEL` | — |

**Text-Limits pro Phase:**

| Phase | Setting | Default | Beschreibung |
|-------|---------|---------|-------------|
| Embedding | `EMBED_MAX_CHARS` | 16000 | Max Zeichen fuer Embedding (Titel + Content, gesamt) |
| Klassifikation | `MAX_DOC_CHARS` | 32000 | Max Zeichen fuer Zieldokument im LLM-Prompt |

## OCR-Korrektur (Vision-LLM)

Konfigurierbar via `OCR_MODE` mit vier Stufen:

| Modus | Beschreibung | Heuristik? | Kosten |
|-------|-------------|------------|--------|
| `off` | Keine OCR-Korrektur (Default) | -- | Keine |
| `text` | Text-only LLM-Korrektur | Ja | 1 LLM-Call |
| `vision_light` | Bild + OCR-Text vergleichen | Ja | 1 Download + N Vision-Calls |
| `vision_full` | Seite-fuer-Seite Korrektur | **Nein** (laeuft immer) | 1 Download + N Vision-Calls |

**Zusaetzliche Einstellungen:**
- `OCR_VISION_MODEL` — Vision-Modell (leer = `OLLAMA_MODEL`). Muss vision-faehig sein.
- `OCR_VISION_MAX_PAGES` — Max Seiten fuer Vision (Default: 3). Gilt fuer `vision_light` und `vision_full`.
- `OCR_VISION_DPI` — Render-Aufloesung fuer PDF-Seiten (Default: 150).

**Wichtig:** Korrigierter Text wird **nie** zurueck nach Paperless geschrieben. Er wird nur lokal in `doc_ocr_cache` gespeichert und fuer Klassifikation + Embedding-Kontext genutzt. `batch_correct_documents()` erlaubt OCR-Korrektur ueber bereits indexierte Dokumente.

**Graceful Degradation:** `vision_full` → `vision_light` → `text` → `off`. Jede Stufe faengt Fehler ab und faellt auf die naechst niedrigere zurueck.

### Reindex mit OCR

`reindex_all()` integriert OCR-Korrektur als Phase vor dem Embedding:

```
reindex_all()
     |
     ├── Embeddings loeschen (doc_embeddings + doc_embedding_meta)
     |
     ├── Phase 0: OCR-Korrektur  (nur wenn OCR_MODE != off)
     │   ├── Alle Docs aus Paperless holen
     │   ├── Fuer jedes Dokument:
     │   │   ├── doc_ocr_cache pruefen → vorhanden? Skip
     │   │   ├── maybe_correct_ocr(doc, ollama, paperless)
     │   │   └── Ergebnis in doc_ocr_cache speichern
     │   └── OCR/Vision-Modell entladen (keep_alive=0)
     |
     ├── Phase 1: Embedding
     │   ├── Fuer jedes Dokument:
     │   │   ├── doc_ocr_cache pruefen → korrigierten Text nutzen
     │   │   └── embed(text) → doc_embeddings
     │   └── Embed-Modell entladen
     |
     └── Fertig
```

Beim wiederholten Reindex (z.B. nach Embedding-Modellwechsel) werden gecachte
OCR-Korrekturen wiederverwendet — nur Embeddings werden neu berechnet.
`doc_ocr_cache` manuell leeren um OCR neu zu erzwingen.

## Worker-Pipeline (Phasen-Ablauf)

`poll_inbox()` verarbeitet Dokumente in Phasen statt einzeln, um Ollama-Modell-Swaps
zu minimieren. Ollama laedt immer nur ein Modell gleichzeitig in den GPU-Speicher —
jeder Modellwechsel kostet mehrere Sekunden (entladen + laden).

```
                         poll_inbox()
                              |
                    +---------+---------+
                    | Phase 0: Prepare  |  kein Modell
                    | Inbox holen,      |
                    | Idempotenz-Filter |
                    | Pending setzen    |
                    +---------+---------+
                              |
               +--------------+--------------+
               | Phase 1: OCR-Korrektur      |  je nach OCR_MODE:
               | (nur wenn OCR_MODE != off)  |  text → OLLAMA_OCR_MODEL
               | Fuer alle Docs: Content     |  vision_* → OCR_VISION_MODEL
               | korrigieren + cachen        |    (oder OLLAMA_MODEL)
               +--+---------+----------------+
                  |         |
                  | unload  |  keep_alive=0
                  |         |
               +--+---------+----------------+
               | Phase 2: Embedding          |  OLLAMA_EMBED_MODEL
               | Fuer alle Docs:             |  (nomic-embed-text-v2-moe)
               |   Text: max EMBED_MAX_CHARS |  num_ctx: OLLAMA_EMBED_NUM_CTX
               |   1x embed() pro Doc        |  (Default: 8192 Tokens)
               |   KNN-Suche → Kontext       |
               |   Embedding merken          |
               +--+---------+----------------+
                  |         |
                  | unload  |  keep_alive=0
                  |         |
               +--+---------+----------------+
               | Phase 3: Klassifikation     |  OLLAMA_MODEL
               | Fuer alle Docs:             |  (gemma4:e2b)
               |   Text: max MAX_DOC_CHARS   |  num_ctx: OLLAMA_NUM_CTX
               |   classify() mit Kontext    |  (Default: 8192 Tokens)
               |   Suggestion speichern      |
               |   Telegram / Auto-Commit    |
               |   Embedding in DB schreiben |  (kein Ollama-Call!)
               +--+---------+----------------+
                  |         |
                  | unload  |  keep_alive=0
                  |         |
                    +-------+-------+
                    | Log-Summary   |
                    +---------------+
```

**Modell-Switches pro Poll-Zyklus:**

```
Vorher (pro Dokument):     Doc1: embed → classify → embed → Doc2: embed → classify → embed → ...
                           = 2*N Switches bei N Dokumenten

Nachher (phasenweise):     [alle embed] → [alle classify]
                           = 1-2 Switches unabhaengig von N

Ohne OCR:        nomic ──────────────> gemma4:e2b                = 1 Switch
Text-OCR:        gemma3:1b ──> nomic ──────────> gemma4:e2b      = 2 Switches
Vision-OCR:      gemma4:e2b ──> nomic ──────────> gemma4:e2b     = 2 Switches*

* Bei vision_light/vision_full ist das OCR-Modell = OLLAMA_MODEL (gemma4:e2b),
  daher wird es nach Phase 1 entladen und fuer Phase 3 wieder geladen.
  Alternativ via OCR_VISION_MODEL ein separates Vision-Modell konfigurieren.
```

**Embedding-Optimierung:** Jedes Dokument wird nur **einmal** embedded (vorher zweimal:
einmal fuer Kontext-Suche, einmal fuer Indexierung). Das Embedding wird in
`_EmbeddingResult` zwischengespeichert und fuer beides wiederverwendet.

**Fehlerbehandlung pro Phase:** Fehler in einer Phase betreffen nur das betroffene
Dokument. Andere Dokumente werden weiterverarbeitet. Wenn Embedding fehlschlaegt,
wird ohne Kontext klassifiziert. Wenn Klassifikation fehlschlaegt, wird das
Embedding trotzdem indexiert (falls vorhanden).

## Wichtige Invarianten

1. **Idempotenz:** Ein Dokument wird pro `updated_at`-Timestamp nur einmal verarbeitet. `processed_documents`-Tabelle haelt State.
2. **Tag-Whitelist-Gate:** `tags`-Updates in Paperless passieren NUR mit IDs, die in der Whitelist stehen. Neue vom LLM vorgeschlagene Tags landen in `tag_whitelist` mit Status `pending`. Abgelehnte Tags werden in `tag_blacklist` verschoben und bei zukuenftigen Vorschlaegen automatisch ignoriert.
3. **Confidence-Gate:** Nur wenn `AUTO_COMMIT_CONFIDENCE > 0` UND das LLM einen Score darueber meldet wird ohne Review committed.
4. **Read-Only bei Fehler:** Wenn Paperless oder Ollama nicht erreichbar sind, wird ein Error-Record geschrieben und der Worker macht weiter. Keine Pipeline-Level-Retries im selben Lauf. Ausnahme: `OllamaClient.embed()` hat HTTP-Level-Retries mit Truncation bei Context-Length-Fehlern und Backoff bei transienten 5xx (konfigurierbar via `OLLAMA_EMBED_RETRIES`).
5. **Inbox-Tag bleibt:** Standardmaessig (`KEEP_INBOX_TAG=true`) wird der `Posteingang`-Tag nach Commit NICHT entfernt. Nur mit `KEEP_INBOX_TAG=false` wird er beim Commit entfernt.
6. **Kontext-Qualitaet:** Nur Dokumente die NICHT mehr im Posteingang sind werden als Kontext fuer neue Klassifikationen genutzt. Inbox-Dokumente sind noch nicht reviewed/approved und wuerden unzuverlaessige Metadaten liefern.
7. **Kontext-Anreicherung:** Kontext-Dokumente enthalten ihre vollstaendige Klassifikation (Korrespondent, Dokumenttyp, Speicherpfad, Tags, Datum). Regel 9 im System-Prompt weist das LLM an, diese Metadaten als starke Hinweise zu nutzen.
8. **Phasen-Pipeline:** `poll_inbox()` verarbeitet alle Dokumente phasenweise (OCR → Embedding → Klassifikation) statt einzeln. Jede Phase nutzt genau ein Ollama-Modell und entlaedt es danach via `keep_alive=0`. Das minimiert VRAM-Verbrauch und Modell-Swaps.
9. **Embedding-Deduplizierung:** Pro Dokument wird `ollama.embed()` genau einmal aufgerufen. Das Ergebnis wird sowohl fuer die KNN-Kontext-Suche als auch fuer die Indexierung wiederverwendet (`_EmbeddingResult`-Dataclass traegt den Vektor zwischen den Phasen).
10. **OCR-Cache:** Korrigierter Text landet in `doc_ocr_cache`, nie in Paperless. Sowohl `poll_inbox()` als auch `reindex_all()` nutzen gecachte Korrekturen. Beim Reindex wird OCR vor dem Embedding als Phase 0 ausgefuehrt — gecachte Eintraege werden uebersprungen.
11. **Embedding-Text-Limit:** `document_summary()` begrenzt den **Gesamttext** (Titel + Content) auf `EMBED_MAX_CHARS` (Default 16000). Die Truncation greift auf die kombinierte Laenge, nicht nur auf den Content-Teil — damit kann ein langer Titel das Limit nicht sprengen.
12. **Settings-Gruppierung:** Die Settings-Seite gruppiert Ollama-Settings nach Pipeline-Phase: "Ollama" (shared: URL, Timeout), "Phase 1: OCR", "Phase 2: Embedding", "Phase 3: Klassifikation". Jede Phase zeigt Modell, Context Window und Text-Limits.

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

## Pre-Commit Checks (WICHTIG)

**Vor jedem Commit muessen diese Checks lokal bestanden werden:**

```bash
ruff check app/ tests/          # Lint
ruff format --check app/ tests/ # Formatting
pytest tests/ -v                # Tests
```

Bei Fehlern: `ruff format app/ tests/` und `ruff check --fix app/ tests/` ausfuehren,
dann erneut pruefen. Erst committen wenn alle drei Checks gruen sind.

Die CI-Pipeline (`lint-and-verify` Job) fuehrt zusaetzlich aus:
- `pip check` (Dependency-Kompatibilitaet)
- `pip-audit` (CVE-Scan)
- `python scripts/check_dependency_age.py --min-days 3` (Supply-Chain)
- Template-Syntax-Check (alle Jinja2-Templates werden geladen)
- `python -c "import app.main"` (Import-Check)
- DB-Schema-Check, Prompt-File-Check

## Telegram-Bot (optional)

Wenn `ENABLE_TELEGRAM=true` und `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` gesetzt:
- Neue Vorschlaege werden als Telegram-Nachricht mit Inline-Keyboard gesendet (Accept / Reject / Edit in GUI)
- Accept/Reject direkt im Chat moeglich, ohne GUI
- Freitext-Nachrichten werden als RAG-Chat-Fragen verarbeitet: Antworten basieren auf aehnlichen Dokumenten aus dem Embedding-Index
- Session-Management pro Chat-ID (1h TTL, max 20 Nachrichten History)
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
- `list_tag_proposals`, `list_blacklisted_tags`
- `classify_document` (rate-limited), `find_similar_documents`
- `get_status`

**Tools (write, opt-in via MCP_ENABLE_WRITE=true):**
- `update_document`, `approve_suggestion`, `reject_suggestion`, `approve_tag`, `unblacklist_tag`

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

### Unsere Strategie: 3-Tage-Mindestalter

Wir installieren **nur Versionen, die mindestens 3 Tage oeffentlich verfuegbar sind**.
Die Logik dahinter:

1. **Erkennungsfenster:** Die meisten kompromittierten Pakete werden innerhalb von
   Stunden bis wenigen Tagen entdeckt und von PyPI entfernt.
2. **Automatische Erkennung:** Tools wie `pip-audit`, Sicherheitsscanner und
   GitHub-Advisories decken bekannte Schwachstellen typischerweise innerhalb
   von 1-3 Tagen auf.
3. **Kosten-Nutzen:** 3 Tage Verzoegerung sind fuer eine Self-Hosted-App akzeptabel —
   lang genug um kompromittierte Releases zu erkennen, kurz genug um
   Security-Patches zeitnah einzuspielen.

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
Veroeffentlichungsdatum und schlaegt fehl, wenn ein Paket juenger als 3 Tage ist:

```bash
python scripts/check_dependency_age.py --min-days 3
```

**Automatische CVE-Fix-Erkennung:** Pakete die juenger als 3 Tage sind werden
automatisch gegen die OSV.dev-Datenbank geprueft. Wenn die Version ein bekanntes
CVE fixt, wird sie automatisch erlaubt — ohne manuellen Allowlist-Eintrag.
Beispiel-Ausgabe:

```
Auto-allowed 1 package(s) (CVE security fixes):
  cryptography==46.0.7 (1d old) fixes CVE-2026-39892
```

Die automatische Erkennung kann mit `--no-auto-cve` deaktiviert werden.

#### 4. `.dependency-age-allowlist` — Manuelle Ausnahmen (Fallback)

Falls die automatische CVE-Erkennung einen validen Security-Fix nicht erkennt
(z.B. weil die OSV-Datenbank noch nicht aktualisiert wurde), kann ein manueller
Eintrag als Fallback dienen:

```
# Security fix for CVE-2026-XXXXX (released YYYY-MM-DD, remove after YYYY-MM-DD)
package==version
```

**Regeln fuer manuelle Ausnahmen:**
- Nur fuer CVE-Fixes von etablierten Paketen.
- Jeder Eintrag muss die CVE-Nummer und das Ablaufdatum enthalten.
- Nach 3 Tagen wird der Eintrag entfernt (das Paket ist dann alt genug).
- In der Regel nicht noetig — die automatische Erkennung deckt >95% der Faelle ab.

#### 5. `.pip-audit-known-vulnerabilities` — Bekannte Audit-Ausnahmen

CVEs in Build-Tools (z.B. `pip` selbst), die keine Laufzeit-Abhaengigkeiten sind:

```
CVE-2025-8869
CVE-2026-1703
```

### Dependency-Update-Workflow

Wenn eine neue Version eines Pakets eingespielt werden soll:

1. **Pruefen:** Ist die Version mindestens 3 Tage alt?
   ```bash
   curl -s https://pypi.org/pypi/<paket>/<version>/json | python3 -c "
   import sys,json; print(json.load(sys.stdin)['urls'][0]['upload_time'])"
   ```
2. **Anheben:** Obergrenze in `pyproject.toml` (direkt) oder `constraints.txt`
   (transitiv) auf die neue Version setzen.
3. **Installieren:** `pip install -c constraints.txt -e ".[dev]"`
4. **Testen:** Alle CI-Checks lokal ausfuehren (Lint, Tests, Audit, Age-Check).
5. **Committen:** Aenderung an `pyproject.toml` / `constraints.txt` committen.

**Bei Security-Patches (< 3 Tage alt):**
- Version in `constraints.txt` anheben.
- Eintrag in `.dependency-age-allowlist` mit CVE-Nummer und Ablaufdatum.
- Nach 3 Tagen: Eintrag aus Allowlist entfernen.

### CI-Pipeline-Uebersicht

```
Install (mit constraints.txt)
  → Ruff Lint + Format
  → Tests (pytest)
  → pip check (Kompatibilitaet)
  → pip-audit (bekannte CVEs)
  → Dependency Age Check (3-Tage-Regel)
  → Template-Syntax
  → Import-Check
  → DB-Schema-Check
  → Prompt-File-Check
  → Docker Build
```

## Bekannte TODOs / Ausbau

- [ ] sqlite-vec HNSW-Index beobachten; vec0 Flat-Scan genuegt bis ~10k Dokumente (sub-5ms bei 768-dim, SIMD-beschleunigt)
- [ ] Re-Embedding-Job wenn das Embedding-Modell wechselt
- [ ] Metrics-Endpoint (Prometheus)
- [ ] Bulk-Approve in der Review-GUI
