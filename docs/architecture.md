# Architektur

Gesamtueberblick ueber den Aufbau und die Datenflussrichtung von ArchiBot.

## System-Kontext

```
                  ┌──────────────┐
                  │   Browser    │
                  └──────┬───────┘
                         │ HTTP
                         ▼
┌────────────────┐    ┌─────────────────────────────────┐    ┌──────────────┐
│ Paperless-NGX  │◀──▶│   ArchiBot                      │◀──▶│    Ollama     │
│                │    │   (FastAPI + Uvicorn)            │    │              │
│ - Dokumente    │    │                                  │    │ - Chat (LLM) │
│ - Metadaten    │    │   Port 8088  (GUI)               │    │ - Embeddings │
│ - Tags         │    │   Port 3001  (MCP, optional)     │    │              │
└────────────────┘    └─────────────────────────────────┘    └──────────────┘
                                     │
                                     ▼
                              ┌──────────────┐
                              │   SQLite +   │
                              │   sqlite-vec │
                              │   (/data)    │
                              └──────────────┘
```

## Dokument-Lebenszyklus

Ein Dokument durchlaeuft folgende Stationen:

```
Paperless: Dokument hochgeladen → Tag "Posteingang" gesetzt
    │
    ▼
┌─────────────────────────────────────────────┐
│  Eingang (eine der drei Varianten)          │
│                                              │
│  1. Worker-Poll  (alle N Sekunden)           │
│  2. Webhook      (POST /webhook/paperless)   │
│  3. Inbox-GUI    (Button "Reprocess")        │
└──────────────────┬──────────────────────────┘
                   │
                   ▼
┌─────────────────────────────────────────────┐
│  Verarbeitungs-Pipeline (_process_document)  │
│                                              │
│  1. Idempotenz-Check (schon verarbeitet?)    │
│  2. OCR-Korrektur  (optional, nur wenn noetig)│
│  3. Kontext-Suche  (aehnliche Dokumente via  │
│     Embedding-Similarity, sqlite-vec)        │
│  4. Klassifikation (Ollama LLM, JSON-Antwort)│
│  5. Vorschlag speichern (suggestions-Tabelle)│
│  6. Telegram-Benachrichtigung (optional)     │
│  7. Auto-Commit (bei hoher Confidence)       │
│  8. Embedding speichern (fuer kuenftige      │
│     Kontext-Suchen)                          │
└──────────────────┬──────────────────────────┘
                   │
                   ▼
┌─────────────────────────────────────────────┐
│  Review (manuell oder automatisch)           │
│                                              │
│  - GUI /review:  Annehmen / Ablehnen /       │
│    Editieren                                 │
│  - Telegram: Accept / Reject Buttons         │
│  - Auto-Commit: wenn Confidence >=           │
│    AUTO_COMMIT_CONFIDENCE                    │
└──────────────────┬──────────────────────────┘
                   │ Accept
                   ▼
┌─────────────────────────────────────────────┐
│  Commit (committer.py)                       │
│                                              │
│  PATCH /api/documents/{id}/ →                │
│   - Titel, Datum, Korrespondent              │
│   - Dokumenttyp, Speicherpfad                │
│   - Tags (merge: bestehende + vorgeschlagene)│
│   - Posteingang-Tag: bleibt (default) oder   │
│     wird entfernt (KEEP_INBOX_TAG=false)     │
│   - Processed-Tag: wird gesetzt (optional)   │
└─────────────────────────────────────────────┘
```

## Einstiegspunkte fuer die Dokumentverarbeitung

Es gibt **fuenf Wege**, wie ein Dokument in die Pipeline gelangt:

| Einstiegspunkt | Ausloeser | Code | Blockiert bei Reindex? |
|---|---|---|---|
| **Worker-Poll** | APScheduler-Intervall (default: 300s) | `worker.py → poll_inbox()` | Ja, ueberspringt mit Log |
| **Webhook** | POST von Paperless nach Consume | `routes/webhook.py` | Ja, antwortet 503 |
| **Inbox-GUI** | Button "Reprocess" in `/inbox` | `routes/inbox.py` | Nein (manuell) |
| **Manueller Poll** | Button "Trigger Poll" in `/settings` | `routes/settings.py → poll_inbox()` | Ja (via poll_inbox Guard) |
| **CLI** | `paperless-classify <cmd>` | `app/cli.py` | Nein (manuell, blockiert bis fertig) |

## Inbox-Seite (`/inbox`)

Die Inbox-Seite zeigt alle Dokumente, die in Paperless den Inbox-Tag tragen:

- **Quelle:** `GET /api/documents/?tags__id__all=<inbox_tag_id>` gegen Paperless
- **Status-Anreicherung:** Fuer jedes Dokument wird der lokale Verarbeitungsstatus aus `processed_documents` abgefragt
- **Status-Badges:**
  - `unprocessed` — Noch nie verarbeitet (grau)
  - `pending` — Klassifiziert, wartet auf Review (gelb)
  - `committed` — Angenommen und geschrieben (gruen)
  - `rejected` — Manuell abgelehnt (rot)
  - `error` — Verarbeitung fehlgeschlagen (rot)
- **Reprocess-Button:** Loescht den bisherigen Verarbeitungsstatus und startet die Pipeline fuer dieses Dokument neu

## Pipeline-Stufen im Detail

### 1. Idempotenz-Check

Prueft in `processed_documents` ob das Dokument bei diesem `updated_at`-Timestamp schon erfolgreich verarbeitet wurde. Dokumente mit Status `error` werden erneut versucht.

### 2. OCR-Korrektur (optional)

Nur aktiv bei `ENABLE_OCR_CORRECTION=true`. Heuristik prueft ob der Text typische OCR-Artefakte enthaelt (viele `?`, einzelne Buchstaben-Woerter). Falls ja, wird der Text via LLM korrigiert — nur im Speicher, Paperless wird nicht veraendert.

### 3. Kontext-Suche

- Berechnet Embedding des Zieldokuments via Ollama (`qwen3-embedding:0.6b`, 1024-dim)
- KNN-Suche in `doc_embeddings` (sqlite-vec) findet die aehnlichsten Dokumente
- **Wichtig:** Dokumente die noch im Posteingang liegen werden als Kontext ausgeschlossen — nur reviewte/bestaetigte Dokumente mit zuverlaessigen Metadaten dienen als Referenz
- Kontext-Dokumente enthalten ihre vollstaendige Klassifikation (Korrespondent, Dokumenttyp, Tags, Speicherpfad)

### 4. Klassifikation

- System-Prompt: Built-in aus `prompts/classify_system.txt` oder Custom Override aus `/data/classify_system.txt`
- User-Prompt: Entity-Listen + Kontext-Dokumente mit Metadaten + Zieldokument
- Token-Budgetierung: 60% fuer Zieldokument, 40% fuer Kontext. Zu kleine Kontext-Dokumente werden gedroppt
- Ollama-Aufruf mit `format: "json"`, liefert strukturiertes JSON
- Ergebnis: Titel, Datum, Korrespondent, Dokumenttyp, Speicherpfad, Tags (mit Confidence), Gesamt-Confidence, Reasoning

### 5. Tag-Whitelist

Vom LLM vorgeschlagene Tags werden gegen die existierenden Paperless-Tags abgeglichen:
- **Bekannte Tags:** Werden direkt mit ihrer ID gespeichert
- **Unbekannte Tags:** Landen in `tag_whitelist` mit Status `pending`. Muessen unter `/tags` manuell freigegeben werden. Bei Freigabe wird der Tag retroaktiv auf bereits committete Dokumente angewendet und in offenen Vorschlaegen voraufgeloest

### 6. Auto-Commit

Wenn `AUTO_COMMIT_CONFIDENCE > 0` und das LLM eine Confidence >= diesem Wert meldet, wird der Vorschlag ohne manuellen Review direkt committed. Bei Auto-Commit wird keine Telegram-Benachrichtigung gesendet.

## Reindex

Der Embedding-Index kann ueber die Settings-Seite komplett neu aufgebaut werden ("Trigger Reindex"):

1. Alle Embeddings werden geloescht (`doc_embeddings` + `doc_embedding_meta`)
2. Alle Dokumente werden aus Paperless geladen
3. Fuer jedes Dokument wird ein neues Embedding berechnet und gespeichert
4. **Fortschritt:** Progress-Bar auf der Settings-Seite (pollt alle 2s), globaler Banner auf allen Seiten
5. **Inbox-Blockade:** Waehrend des Reindex werden Worker-Poll und Webhook blockiert, um Raceconditions mit teilweise aufgebauten Embeddings zu vermeiden

## Datenbank-Schema

| Tabelle | Zweck |
|---|---|
| `processed_documents` | Verarbeitungsstatus pro Dokument (Idempotenz) |
| `suggestions` | LLM-Vorschlaege (original vs. proposed, Status pending/committed/rejected) |
| `doc_embeddings` | Virtuelle sqlite-vec Tabelle fuer Vektor-Similarity (1024-dim) |
| `doc_embedding_meta` | Metadaten zu Embeddings (document_id, title, created_at) |
| `tag_whitelist` | Staging fuer unbekannte Tags (name, times_seen, approved) |
| `tag_blacklist` | Abgelehnte Tags — werden bei zukuenftigen Vorschlaegen ignoriert |
| `doc_ocr_cache` | Lokal gecachter korrigierter OCR-Text (nie zurueck nach Paperless) |
| `doc_fts` | FTS5 Volltext-Suchindex (title, content) fuer Hybrid-Suche |
| `errors` | Fehler-Audit-Trail (stage, document_id, message) |
| `audit_log` | Aktions-Audit-Trail (commit, reject, prompt_update) |
| `poll_cycles` | Zusammenfassung pro `poll_inbox()`-Aufruf (started_at, finished_at, succeeded, failed, skipped) |
| `phase_timing` | Pro-Dokument-Pro-Phase Verarbeitungsdauer (poll_cycle_id, phase, duration_ms, success) |

## Docker-Deployment

- **Ein Container:** GUI + Worker + optional MCP Server
- **Ports:** 8088 (GUI), 3001 (MCP, optional)
- **Volume:** `/data` fuer SQLite-DB, Logs, Custom Prompt
- **Netzwerk:** Muss Paperless und Ollama erreichen koennen. Bei separaten Compose-Stacks: externe Netzwerke einkommentieren in `docker-compose.yml`
