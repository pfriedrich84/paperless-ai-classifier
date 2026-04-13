---
description: "Dependency-Update mit 14-Tage-Supply-Chain-Pruefung"
argument-hint: "<paketname> [version]"
---

Aktualisiere eine Python-Dependency unter Einhaltung der 14-Tage-Supply-Chain-Regel.

**Argument:** $ARGUMENTS (Paketname, optional mit Zielversion)

**Workflow:**

1. **Version ermitteln:** Falls keine Version angegeben, die neueste Version von PyPI holen:
   ```
   curl -s https://pypi.org/pypi/<paket>/json | python3 -c "import sys,json; print(json.load(sys.stdin)['info']['version'])"
   ```

2. **Alter pruefen:** Upload-Datum der Version von PyPI abfragen:
   ```
   curl -s https://pypi.org/pypi/<paket>/<version>/json | python3 -c "import sys,json; print(json.load(sys.stdin)['urls'][0]['upload_time'])"
   ```
   - Falls juenger als 14 Tage: **Abbrechen** mit Warnung. Nur bei CVE-Fix fortfahren (dann `.dependency-age-allowlist` aktualisieren).
   - Falls aelter als 14 Tage: Weiter.

3. **Obergrenze anheben:**
   - Direkte Dependency → `pyproject.toml`: `<=`-Obergrenze auf neue Version setzen
   - Transitive Dependency → `constraints.txt`: Version-Pin aktualisieren

4. **Installieren:** `pip install -c constraints.txt -e ".[dev]"`

5. **Alle Checks ausfuehren:**
   - `ruff check app/ tests/`
   - `ruff format --check app/ tests/`
   - `pytest tests/ -v`
   - `python scripts/check_dependency_age.py --min-days 14`

6. **Zusammenfassung:** Zeige was geaendert wurde und ob alle Checks bestanden haben.

**Wichtig:** Nicht automatisch committen — nur die Aenderungen vorbereiten und das Ergebnis berichten.
