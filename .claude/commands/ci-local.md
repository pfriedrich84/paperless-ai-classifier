---
description: "Lokale CI-Simulation (alle Checks wie in GitHub Actions)"
---

Fuehre alle CI-Checks lokal aus, in der gleichen Reihenfolge wie `.github/workflows/ci.yml`:

1. **Ruff Lint:** `ruff check app/ tests/`
2. **Ruff Format:** `ruff format --check app/ tests/`
3. **Tests:** `pytest tests/ -v`
4. **Dependency-Kompatibilitaet:** `pip check`
5. **CVE-Scan:** `pip-audit -r pyproject.toml --desc --ignore-vuln $(cat .pip-audit-known-vulnerabilities | grep -v '^#' | grep -v '^$' | tr '\n' ' ' | sed 's/ / --ignore-vuln /g')` (oder aehnlich, passend zur ci.yml)
6. **Dependency Age:** `python scripts/check_dependency_age.py --min-days 14`
7. **Template-Syntax:** `python -c "from jinja2 import Environment, FileSystemLoader; env = Environment(loader=FileSystemLoader('app/templates')); [env.get_template(t) for t in env.list_templates()]"`
8. **Import-Check:** `python -c "import app.main"`

Jeden Check einzeln ausfuehren und das Ergebnis (bestanden/fehlgeschlagen) dokumentieren.
Am Ende eine Zusammenfassung aller Checks mit Status-Uebersicht ausgeben.

Falls ein Check fehlschlaegt: die verbleibenden Checks trotzdem ausfuehren (nicht abbrechen),
damit die vollstaendige Uebersicht sichtbar ist.
