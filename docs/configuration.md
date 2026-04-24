# Konfiguration

Alle Einstellungen werden ueber Umgebungsvariablen gesteuert. Primaer ueber
`.env` (Docker Compose) oder die Settings-UI (`/settings`), die Aenderungen
in `config.env` persistiert.

> Hinweis: Die mitgelieferte `.env.example` nutzt ein 6GB-VRAM-Preset
> (staerkere Embedding/OCR-Modelle). Die Tabellen unten dokumentieren die
> internen App-Defaults.

**Prioritaet (hoechste zuerst):**
1. `{DATA_DIR}/config.env` — geschrieben von der Settings-UI
2. OS-Umgebungsvariablen — gesetzt von Docker Compose aus `.env`
3. `.env`-Datei — Fallback fuer lokale Entwicklung
4. Defaults — in der Anwendung hinterlegt

## Paperless-NGX

| Variable | Default | Beschreibung |
|---|---|---|
| `PAPERLESS_URL` | — | Basis-URL, z.B. `http://paperless:8000` |
| `PAPERLESS_TOKEN` | — | API-Token (Paperless → Admin → Tokens) |
| `PAPERLESS_INBOX_TAG_ID` | — | ID des Tags `Posteingang` |
| `PAPERLESS_PROCESSED_TAG_ID` | — | Optional: Tag-ID, die nach Commit gesetzt wird |
| `KEEP_INBOX_TAG` | `true` | Posteingang-Tag nach Commit beibehalten |

## Ollama (allgemein)

| Variable | Default | Beschreibung |
|---|---|---|
| `OLLAMA_URL` | `http://ollama:11434` | Ollama-Endpoint |
| `OLLAMA_TIMEOUT_SECONDS` | `600` | HTTP-Timeout fuer Ollama-Requests (Sekunden) |
| `OLLAMA_CHAT_RETRIES` | `2` | Max. Retries fuer Chat/OCR/Klassifikation bei transienten Fehlern (429/5xx/Timeouts) |
| `OLLAMA_CHAT_RETRY_BASE_DELAY` | `1.0` | Basis-Delay in Sekunden fuer exponentiellen Chat-Backoff |
| `OLLAMA_MODEL_SWAP_DELAY` | `8.0` | Wartezeit nach Model-Unload, damit Ollama freie VRAM korrekt erkennt |

## Phase 1: OCR-Korrektur

| Variable | Default | Beschreibung |
|---|---|---|
| `OCR_MODE` | `off` | OCR-Stufe: `off`, `text`, `vision_light`, `vision_full` |
| `OLLAMA_OCR_MODEL` | `qwen3:4b` | Modell fuer Text-Only OCR-Korrektur |
| `OCR_VISION_MODEL` | `qwen3-vl:4b` | Vision-Modell fuer OCR (muss vision-faehig sein) |
| `OCR_VISION_MAX_PAGES` | `3` | Max. Seiten fuer Vision-OCR |
| `OCR_VISION_DPI` | `150` | Render-Aufloesung fuer PDF-Seiten (Pixel pro Zoll) |
| `OLLAMA_OCR_NUM_CTX` | `12288` | Kontextfenster fuer OCR-Modelle (Tokens). Vision braucht ~1536 Tokens/Seite. |

### OCR-Modi im Vergleich

| Modus | Beschreibung | Heuristik? | Kosten |
|-------|-------------|------------|--------|
| `off` | Keine OCR-Korrektur (Default) | — | Keine |
| `text` | Text-only LLM-Korrektur | Ja | 1 LLM-Call |
| `vision_light` | Bild + OCR-Text vergleichen | Ja | 1 Download + N Vision-Calls |
| `vision_full` | Seite-fuer-Seite Korrektur | Nein (laeuft immer) | 1 Download + N Vision-Calls |

**Graceful Degradation:** `vision_full` → `vision_light` → `text` → `off`.
Jede Stufe faengt Fehler ab und faellt auf die naechst niedrigere zurueck.

## Phase 2: Embedding

| Variable | Default | Beschreibung |
|---|---|---|
| `OLLAMA_EMBED_MODEL` | `qwen3-embedding:4b` | Embedding-Modell (hoehere Retrieval-Qualitaet) |
| `OLLAMA_EMBED_DIM` | `0` | Embedding-Dimension fuer sqlite-vec. `0` = Auto (`qwen3-embedding:0.6b`→1024, `qwen3-embedding:4b`→2560). |
| `OLLAMA_EMBED_NUM_CTX` | `8192` | Kontextfenster fuer das Embedding-Modell (Tokens) |
| `EMBED_MAX_CHARS` | `6000` | Max. Zeichen des Dokumenttexts fuer Embedding |
| `OLLAMA_EMBED_RETRIES` | `3` | Max. Retries bei Embedding-Fehlern (Truncation + transiente 500er) |
| `OLLAMA_EMBED_RETRY_BASE_DELAY` | `1.0` | Basis-Delay in Sekunden fuer exponentiellen Backoff |

## Phase 3: Klassifikation

| Variable | Default | Beschreibung |
|---|---|---|
| `OLLAMA_MODEL` | `gemma4:e4b` | Klassifikations-Modell (6GB-Empfehlung; Alternativen: `qwen3:4b`) |
| `OLLAMA_NUM_CTX` | `16384` | Kontextfenster fuer das Chat-Modell (Tokens) |
| `MAX_DOC_CHARS` | `24000` | Max. Zeichen des Dokumenttexts im LLM-Prompt |
| `CONTEXT_MAX_DOCS` | `5` | Wieviele aehnliche Dokumente als Few-Shot-Kontext |
| `AUTO_COMMIT_CONFIDENCE` | `0` | 0 = immer manuell reviewen. Ab diesem Score (1–100) automatisch committen. |
| `ENABLE_JUDGE_VERIFICATION` | `false` | Zweiter LLM-Pass, der jede Klassifikation prueft und ggf. korrigiert. Laeuft nur bei niedriger Confidence und vorhandenem Kontext. |
| `JUDGE_CONFIDENCE_THRESHOLD` | `85` | Judge-Pass wird uebersprungen, wenn die Initial-Confidence bereits >= diesem Wert (0–100) ist. |
| `OLLAMA_JUDGE_MODEL` | — | Optionales Modell fuer den Judge-Pass. Leer = `OLLAMA_MODEL` wiederverwenden (kein zusaetzlicher GPU-Swap). |

## Worker

| Variable | Default | Beschreibung |
|---|---|---|
| `POLL_INTERVAL_SECONDS` | `300` | Sekunden zwischen Inbox-Polls |

## GUI

| Variable | Default | Beschreibung |
|---|---|---|
| `GUI_PORT` | `8088` | Port der Review-GUI |
| `GUI_BASE_URL` | — | Externe URL fuer Telegram-Links (z.B. `https://classifier.local:8088`) |
| `GUI_DATE_FORMAT` | `%d.%m.%Y` | Anzeigeformat fuer Datumswerte in der GUI (Python-`strftime`-Syntax) |
| `GUI_USERNAME` | — | Basic-Auth Benutzername (leer = deaktiviert) |
| `GUI_PASSWORD` | — | Basic-Auth Passwort |

## Telegram (optional)

| Variable | Default | Beschreibung |
|---|---|---|
| `ENABLE_TELEGRAM` | `false` | Telegram-Bot aktivieren |
| `TELEGRAM_BOT_TOKEN` | — | Bot-Token von @BotFather |
| `TELEGRAM_CHAT_ID` | — | Chat/Gruppen-ID fuer Benachrichtigungen |
| `TELEGRAM_POLL_INTERVAL` | `5` | Sekunden zwischen Telegram-getUpdates-Calls |

## Webhook (optional)

| Variable | Default | Beschreibung |
|---|---|---|
| `WEBHOOK_SECRET` | — | Shared Secret fuer `POST /webhook/paperless`. Siehe [Webhook-Doku](./webhooks.md). |

## MCP Server (optional)

| Variable | Default | Beschreibung |
|---|---|---|
| `ENABLE_MCP` | `false` | MCP-Server im selben Container mitlaufen lassen |
| `MCP_TRANSPORT` | `stdio` | Transport: `stdio`, `sse`, `streamable-http` |
| `MCP_PORT` | `3001` | Port fuer SSE/HTTP-Transport |
| `MCP_HOST` | `0.0.0.0` | Bind-Adresse |
| `MCP_ENABLE_WRITE` | `false` | Write-Tools aktivieren |
| `MCP_API_KEY` | — | API-Key fuer Authentifizierung (empfohlen bei SSE) |
| `MCP_CLASSIFY_RATE_LIMIT` | `10` | Max. KI-Klassifikationen pro Stunde (0 = unbegrenzt) |

Details: [MCP-Server-Dokumentation](./mcp.md)

## System

| Variable | Default | Beschreibung |
|---|---|---|
| `DATA_DIR` | `/data` | Persistentes Datenverzeichnis (DB, Config) |
| `LOG_LEVEL` | `INFO` | Log-Level: `DEBUG`, `INFO`, `WARNING`, `ERROR` |

## Settings-UI

Die meisten Variablen lassen sich auch ueber die Web-Oberflaeche aendern
(`/settings`). Aenderungen werden in `{DATA_DIR}/config.env` gespeichert
und sind sofort wirksam — kein Container-Neustart noetig.

Ausnahmen, die einen Neustart erfordern, sind in der UI entsprechend markiert
(z.B. `GUI_PORT`, `MCP_TRANSPORT`).
