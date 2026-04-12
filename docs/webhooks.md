# Webhook-Konfiguration

Anleitung zur Einrichtung eines Webhooks, damit Paperless-NGX den Classifier sofort nach dem Einlesen eines Dokuments benachrichtigt — als Alternative oder Ergaenzung zum Polling.

## Ueberblick

Standardmaessig pollt der Worker alle `POLL_INTERVAL_SECONDS` (Default: 300s = 5 Minuten) die Inbox. Mit einem Webhook wird die Verarbeitung **sofort** nach dem Consume ausgeloest, ohne auf den naechsten Poll zu warten.

**Beide Methoden koennen parallel laufen.** Der Idempotenz-Check verhindert, dass ein Dokument doppelt verarbeitet wird.

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

Der Webhook-Endpoint ist sofort aktiv — es gibt keinen Feature-Toggle. Die Authentifizierung greift nur, wenn `WEBHOOK_SECRET` gesetzt ist.

## 2. Paperless-NGX konfigurieren

Paperless unterstuetzt nativ keine Webhooks als Konfigurationsoption in der GUI. Es gibt zwei Moeglichkeiten:

### Variante A: Post-Consume-Script

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
        "${CLASSIFIER_URL}/webhook/paperless" \
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

### Variante B: Custom Consumer mit Python

Fuer fortgeschrittene Setups kann ein Paperless Custom Consumer verwendet werden. Siehe [Paperless-NGX Doku: Post-Consume](https://docs.paperless-ngx.com/advanced_usage/#post-consume-script).

## 3. Netzwerk-Setup

Der Classifier muss fuer Paperless ueber das Netzwerk erreichbar sein.

### Gleicher Docker-Compose-Stack

Wenn Paperless und der Classifier im selben `docker-compose.yml` laufen, koennen sie sich ueber den Service-Namen erreichen:

```
http://paperless-ai-classifier:8088/webhook/paperless
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

**Adresse im Script:**

```
http://paperless-ai-classifier:8088/webhook/paperless
```

### Verschiedene Hosts

Wenn Paperless und der Classifier auf verschiedenen Maschinen laufen, muss der Classifier-Port (default: 8088) erreichbar sein:

```
http://classifier-host:8088/webhook/paperless
```

> **Hinweis:** Der Webhook-Endpoint ist nicht durch Basic-Auth geschuetzt (bewusst, damit Paperless ohne Credentials senden kann). Die Authentifizierung erfolgt ausschliesslich ueber den `X-Webhook-Secret` Header.

## Webhook-Referenz

### Endpoint

```
POST /webhook/paperless
```

### Request

**Header:**

| Header | Wert | Pflicht? |
|---|---|---|
| `Content-Type` | `application/json` | Ja |
| `X-Webhook-Secret` | Wert von `WEBHOOK_SECRET` | Nur wenn `WEBHOOK_SECRET` gesetzt |

**Body:**

```json
{
  "document_id": 123
}
```

### Responses

| Status | Bedeutung |
|---|---|
| `200` | Verarbeitung erfolgreich (oder Fehler im Body) |
| `403` | Webhook-Secret ungueltig |
| `422` | Body ungueltig (fehlende/falsche `document_id`) |
| `503` | Reindex laeuft gerade — spaeter erneut versuchen |

**Erfolg:**

```json
{
  "status": "ok",
  "document_id": 123
}
```

**Verarbeitungsfehler (HTTP 200, aber Fehler im Body):**

```json
{
  "status": "error",
  "document_id": 123,
  "error": "Fehlermeldung"
}
```

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
- `X-Webhook-Secret` Header im Script
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
| **Setup** | Keine Konfiguration noetig | Script + Netzwerk noetig |
| **Empfehlung** | Immer aktiv als Fallback | Zusaetzlich fuer schnelle Reaktion |

**Empfohlenes Setup:** Beides aktiviert. Der Webhook sorgt fuer sofortige Verarbeitung, der Poll dient als Sicherheitsnetz falls ein Webhook verloren geht.
