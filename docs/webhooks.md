# Webhook-Konfiguration

Anleitung zur Einrichtung von Webhooks, damit Paperless-NGX den Classifier sofort nach dem Einlesen oder Aendern eines Dokuments benachrichtigt — als Alternative oder Ergaenzung zum Polling.

## Ueberblick

Standardmaessig pollt der Worker alle `POLL_INTERVAL_SECONDS` (Default: 300s = 5 Minuten) die Inbox. Mit Webhooks wird die Verarbeitung **sofort** ausgeloest, ohne auf den naechsten Poll zu warten.

**Zwei Webhook-Endpoints:**

| Endpoint | Zweck |
|----------|-------|
| `POST /webhook/new` | Volle Pipeline: OCR + Embedding + Klassifikation + Suggestion |
| `POST /webhook/edit` | Nur Embedding-Update (keine Klassifikation) |

**Polling und Webhooks koennen parallel laufen.** Der Idempotenz-Check verhindert, dass ein Dokument doppelt verarbeitet wird.

## Voraussetzungen

- Paperless-NGX >= 2.0
- Der Classifier-Container muss fuer Paperless erreichbar sein (gleiches Docker-Netzwerk oder Netzwerk-Route)
- Optional: ein Webhook-Secret fuer Authentifizierung

## 1. Classifier konfigurieren

In der `.env` des Classifiers:

```env
# Webhook-Secret (empfohlen). Leerer String = keine Authentifizierung.
WEBHOOK_SECRET=mein-geheimer-webhook-token
```

Die Webhook-Endpoints sind sofort aktiv — es gibt keinen Feature-Toggle. Die Authentifizierung greift nur, wenn `WEBHOOK_SECRET` gesetzt ist.

## 2. Paperless-NGX konfigurieren

Es gibt drei Moeglichkeiten, den Classifier per Webhook zu triggern:

### Variante A: Workflow-Webhook (empfohlen, ab Paperless >= 2.x)

Paperless-NGX unterstuetzt **Workflow-Webhooks** direkt in der GUI. Diese Methode
erfordert kein Script und keine Datei-Mounts.

**Zwei Workflows konfigurieren:**

| Workflow | Trigger | Webhook-URL | Zweck |
|----------|---------|-------------|-------|
| 1 | Dokument hinzugefuegt | `http://<host>:8088/webhook/new` | Volle Verarbeitung (OCR + Embedding + Klassifikation) |
| 2 | Dokument geaendert | `http://<host>:8088/webhook/edit` | Nur Embedding-Update (keine Klassifikation) |

**Einstellungen pro Workflow:**

- **Aktionstyp:** Webhook
- **Webhook-URL:** siehe Tabelle oben
- **Webhook-Payload als JSON senden:** AN
- **Dokument einbeziehen:** AN (optional — der Classifier holt das Dokument sowieso via API)
- **Webhook-Kopfzeilen:** `X-Webhook-Secret: <WEBHOOK_SECRET>` (wenn konfiguriert)

**Payload-Format** (wird automatisch von Paperless gesendet):

```json
{
  "event": "document_created",
  "object": {
    "id": 123,
    "correspondent": "Example Corp",
    "document_type": "Invoice",
    "storage_path": null,
    "tags": [1, 5],
    "created": "2026-04-14",
    "content": "...raw text content...",
    "mime_type": "application/pdf",
    "filename": "2026-04-14_example_corp.pdf"
  }
}
```

Beide Endpoints akzeptieren sowohl dieses Workflow-Format als auch das Legacy-Format
(`{"document_id": 123}`).

### Variante B: Post-Consume-Script

Paperless kann nach dem Einlesen eines Dokuments ein Shell-Script ausfuehren. Dieses Script sendet den Webhook per `curl`.

**1. Script erstellen** (z.B. `/opt/paperless/scripts/notify-classifier.sh`):

```bash
#!/bin/bash
# Wird von Paperless nach dem Consume aufgerufen.
# Umgebungsvariable DOCUMENT_ID wird von Paperless gesetzt.

CLASSIFIER_URL="http://paperless-ai-classifier:8088"
WEBHOOK_SECRET="mein-geheimer-webhook-token"

if [ -n "$DOCUMENT_ID" ]; then
    curl -s -X POST \
        -H "Content-Type: application/json" \
        -H "X-Webhook-Secret: ${WEBHOOK_SECRET}" \
        -d "{\"document_id\": ${DOCUMENT_ID}}" \
        "${CLASSIFIER_URL}/webhook/new" \
        --max-time 10 \
        || true  # Fehler ignorieren, damit Paperless nicht blockiert
fi
```

**2. Script ausfuehrbar machen:**

```bash
chmod +x /opt/paperless/scripts/notify-classifier.sh
```

**3. In Paperless konfigurieren** (`docker-compose.env` oder `.env`):

```env
PAPERLESS_POST_CONSUME_SCRIPT=/opt/paperless/scripts/notify-classifier.sh
```

**4. Script in den Paperless-Container mounten** (`docker-compose.yml`):

```yaml
services:
  paperless:
    volumes:
      - ./scripts/notify-classifier.sh:/opt/paperless/scripts/notify-classifier.sh:ro
```

**5. Paperless neu starten.**

### Variante C: Custom Consumer mit Python

Fuer fortgeschrittene Setups kann ein Paperless Custom Consumer verwendet werden. Siehe [Paperless-NGX Doku: Post-Consume](https://docs.paperless-ngx.com/advanced_usage/#post-consume-script).

## 3. Netzwerk-Setup

Der Classifier muss fuer Paperless ueber das Netzwerk erreichbar sein.

### Gleicher Docker-Compose-Stack

Wenn Paperless und der Classifier im selben `docker-compose.yml` laufen, koennen sie sich ueber den Service-Namen erreichen:

```
http://paperless-ai-classifier:8088/webhook/new
http://paperless-ai-classifier:8088/webhook/edit
```

### Separate Docker-Compose-Stacks

Wenn Paperless und der Classifier in unterschiedlichen Stacks laufen, muessen sie ein gemeinsames Docker-Netzwerk teilen.

**Im Classifier `docker-compose.yml`:**

```yaml
services:
  paperless-ai-classifier:
    networks:
      - paperless

networks:
  paperless:
    external: true
    name: ix-paperless-ngx_default   # Name des Paperless-Netzwerks
```

### Verschiedene Hosts

Wenn Paperless und der Classifier auf verschiedenen Maschinen laufen, muss der Classifier-Port (default: 8088) erreichbar sein:

```
http://classifier-host:8088/webhook/new
http://classifier-host:8088/webhook/edit
```

> **Hinweis:** Die Webhook-Endpoints sind nicht durch Basic-Auth geschuetzt (bewusst, damit Paperless ohne Credentials senden kann). Die Authentifizierung erfolgt ausschliesslich ueber den `X-Webhook-Secret` Header.

## Webhook-Referenz

### POST /webhook/new — Volle Verarbeitung

Verarbeitet ein Dokument mit der vollen Pipeline: OCR-Korrektur, Embedding,
Klassifikation, Suggestion-Erstellung, optional Auto-Commit und Telegram.

**Header:**

| Header | Wert | Pflicht? |
|---|---|---|
| `Content-Type` | `application/json` | Ja |
| `X-Webhook-Secret` | Wert von `WEBHOOK_SECRET` | Nur wenn `WEBHOOK_SECRET` gesetzt |

**Body** (Workflow-Format oder Legacy-Format):

```json
{"event": "document_created", "object": {"id": 123, ...}}
```

```json
{"document_id": 123}
```

**Responses:**

| Status | Bedeutung |
|---|---|
| `200` | Verarbeitung erfolgreich (oder Fehler im Body) |
| `403` | Webhook-Secret ungueltig |
| `422` | Body ungueltig (fehlende/falsche `document_id`) |
| `503` | Reindex laeuft gerade — spaeter erneut versuchen |

### POST /webhook/edit — Nur Embedding-Update

Berechnet nur das Embedding eines Dokuments neu (mit optionaler OCR-Korrektur).
Keine Klassifikation, keine Suggestion, kein Telegram. Nutzen: wenn ein Dokument
in Paperless geaendert wurde und der Embedding-Index aktualisiert werden soll.

**Header und Body:** Identisch zu `/webhook/new`.

**Responses:**

| Status | Bedeutung |
|---|---|
| `200` | `{"status": "ok", "document_id": 123, "action": "reembedded"}` |
| `200` | `{"status": "ok", "document_id": 123, "action": "skipped_empty"}` (leerer Content) |
| `403` | Webhook-Secret ungueltig |
| `422` | Body ungueltig |
| `500` | Embedding-Fehler (z.B. Ollama nicht erreichbar) |
| `503` | Reindex laeuft gerade |

## Paperless-Umgebungsvariablen im Post-Consume-Script

Paperless setzt folgende Umgebungsvariablen, die im Script verfuegbar sind:

| Variable | Beschreibung |
|---|---|
| `DOCUMENT_ID` | ID des verarbeiteten Dokuments |
| `DOCUMENT_FILE_NAME` | Original-Dateiname |
| `DOCUMENT_CREATED` | Erstellungsdatum |
| `DOCUMENT_ADDED` | Hinzugefuegt-am-Datum |
| `DOCUMENT_ARCHIVE_PATH` | Pfad zur archivierten Version |
| `DOCUMENT_ORIGINAL_FILENAME` | Originaler Dateiname beim Upload |

Fuer den Classifier ist nur `DOCUMENT_ID` relevant.

## Fehlerbehebung

### Webhook kommt nicht an

1. **Netzwerk pruefen:** Kann Paperless den Classifier erreichen?
   ```bash
   # Aus dem Paperless-Container heraus testen:
   docker exec paperless curl -s http://paperless-ai-classifier:8088/healthz
   # Erwartete Antwort: {"status":"ok"}
   ```

2. **Script-Ausfuehrung pruefen:** Hat Paperless das Script ausgefuehrt?
   ```bash
   # Paperless-Logs pruefen:
   docker logs paperless 2>&1 | grep -i "post.consume"
   ```

3. **Script-Berechtigungen:** Ist das Script ausfuehrbar?
   ```bash
   docker exec paperless ls -la /opt/paperless/scripts/notify-classifier.sh
   ```

### 403 Forbidden

Das Webhook-Secret stimmt nicht ueberein. Pruefen:
- `WEBHOOK_SECRET` in der Classifier `.env`
- `X-Webhook-Secret` Header im Script bzw. in den Workflow-Kopfzeilen
- Keine Leerzeichen oder Zeilenumbrueche im Secret

### 503 Service Unavailable

Ein Reindex laeuft gerade. Das Dokument wird beim naechsten regulaeren Poll verarbeitet, sobald der Reindex abgeschlossen ist.

### Dokument wird nicht verarbeitet

- Hat das Dokument den Inbox-Tag (`PAPERLESS_INBOX_TAG_ID`)?
- Wurde es bereits verarbeitet? (Idempotenz-Check — pruefen unter `/inbox`)
- Button "Reprocess" in der Inbox-GUI erzwingt erneute Verarbeitung

## Webhook vs. Polling

| | Polling | Webhook |
|---|---|---|
| **Latenz** | Bis zu `POLL_INTERVAL_SECONDS` | Sofort nach Consume |
| **Zuverlaessigkeit** | Sehr hoch (holt alles nach) | Abhaengig von Netzwerk |
| **Setup** | Keine Konfiguration noetig | Workflow oder Script noetig |
| **Empfehlung** | Immer aktiv als Fallback | Zusaetzlich fuer schnelle Reaktion |

**Empfohlenes Setup:** Beides aktiviert. Der Webhook sorgt fuer sofortige Verarbeitung, der Poll dient als Sicherheitsnetz falls ein Webhook verloren geht.
