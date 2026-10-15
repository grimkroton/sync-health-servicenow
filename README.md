# servicehealth-sync

Zieht Azure Service Health Events per REST API ab und sendet sie im **Common Alert Schema**
an den generischen Webhook des ServiceNow Event Management (Authentifizierung per Bearer Token).

## Ablauf eines Laufs
1. Zustand laden (`state.json`, sonst Artifact des letzten erfolgreichen GitLab-Laufs, sonst leer).
2. Pro Subscription die Events abrufen, die seit dem letzten Lauf (minus Überlappung) aktualisiert wurden.
3. Nur neue oder geänderte Events (Status oder `lastUpdateTime`) mappen und senden.
4. Zustand schreiben. `last_run` rückt nur bei fehlerfreiem Lauf vor.

Der Status folgt Microsoft: `Active` → `Fired`, `Resolved` → `Resolved`.

## Konfiguration (Umgebungsvariablen / GitLab CI/CD-Variablen, Secrets als *masked*)
| Variable | Bedeutung |
|---|---|
| `AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET` | Service Principal mit Rolle *Reader* |
| `AZURE_SUBSCRIPTION_IDS` | Komma- oder Semikolon-getrennt |
| `SERVICENOW_INSTANCE` | Host, z. B. `instanz.service-now.com` |
| `SERVICENOW_SOURCE` | Wert für `source=` in der Webhook-URL |
| `SERVICENOW_CONNECTOR_SYS_ID` | Wert für `sys_id=` in der Webhook-URL |
| `SERVICENOW_TOKEN` | Bearer Token |
| `LOOKBACK_DAYS` (30), `OVERLAP_HOURS` (24) | Rückblick beim ersten Lauf, Sicherheits-Überlappung |
| `MAX_TEXT_LENGTH` (3000), `MAX_REGIONS` (10) | Kürzung von Text und Regionsliste |
| `ALERT_RULE_NAME` (`ServiceHealth-Pull`) | Wert für `essentials.alertRule` |
| `DRY_RUN` (`false`) | `true`: Payloads nur ausgeben, nichts senden, Zustand nicht ändern |
| `HTTPS_PROXY`, `NO_PROXY` | Proxy, wird von `requests` automatisch beachtet |

## Lokal testen
```bash
pip install -r requirements.txt
python -m unittest discover -s tests -t .
# Trockenlauf gegen echte Azure-Daten (sendet nichts an ServiceNow):
export AZURE_TENANT_ID=... AZURE_CLIENT_ID=... AZURE_CLIENT_SECRET=... AZURE_SUBSCRIPTION_IDS=...
export SERVICENOW_INSTANCE=x SERVICENOW_SOURCE=x SERVICENOW_CONNECTOR_SYS_ID=x SERVICENOW_TOKEN=x
python -m servicehealth_sync --dry-run
```
