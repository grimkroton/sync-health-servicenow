# servicehealth-sync

Zieht Azure Service Health Events per REST API (Resource Health Events) ab und sendet sie als einzelne Events
an den generischen Webhook des ServiceNow Event Management
(`/api/sn_em_connector/em/inbound_event`, Authentifizierung per API-Key im Header `x-sn-apikey`).

## Ablauf eines Laufs
1. Zustand laden (`state.json`, sonst Artifact des letzten erfolgreichen GitLab-Laufs, sonst leer).
2. Alle sichtbaren, aktiven Subscriptions ermitteln (`GET /subscriptions`) und pro Subscription die Events abrufen, die seit dem letzten Lauf (minus Überlappung) aktualisiert wurden.
3. Nur neue oder geänderte Events (Status oder `lastUpdateTime`) mappen und senden. Pro betroffener Subscription entsteht ein eigenes Event, jedes Event ist ein eigener Request.
4. Offene Health Advisories, deren Enddatum erreicht ist, werden geschlossen (siehe Status).
5. Zustand schreiben. `last_run` rückt nur bei fehlerfreiem Lauf vor.

Neue Subscriptions werden automatisch erkannt und einmalig mit vollem Rückblick (`LOOKBACK_DAYS`) abgefragt,
damit bereits aktive Events nicht verpasst werden. Welche Subscriptions gelten, bestimmt die Rolle *Reader*
des Service Principals (z. B. auf Management-Group-Ebene vergeben, dann sind neue Subscriptions automatisch dabei).

Für jeden Request an ServiceNow steht im Job-Log der HTTP-Status samt Antwort (`ServiceNow: HTTP 200 …`).
Weiterleitungen (3xx) werden nicht verfolgt und als Fehler gewertet.

## Mapping auf das ServiceNow-Event

Ein Request enthält genau ein Event (flaches JSON-Objekt, alle Werte sind Strings):

| ServiceNow-Feld | Quelle in der Azure-API |
|---|---|
| `source` | `properties.eventSource` (z. B. `ServiceHealth`) |
| `event_class` | konstant `Azure Service Health` |
| `node` | `impact[].impactedRegions[].impactedSubscriptions[]`: **pro Subscription ein eigenes Event** |
| `resource` | `impactedService` (bei mehreren Diensten kommagetrennt, nur die der jeweiligen Subscription) |
| `metric_name` | leer |
| `type` | `properties.eventType` (`ServiceIssue`, `HealthAdvisory`, `PlannedMaintenance`, `SecurityAdvisory`, …) |
| `severity` | siehe Tabelle unten |
| `description` | `properties.title` (HTML-Entities bereinigt) |
| `message_key` | `id` des Events, z. B. `Microsoft.ResourceHealth/events/7QL5-Z50` |
| `time_of_event` | `properties.lastUpdateTime` (UTC, `yyyy-MM-dd HH:mm:ss`); bei Schließen per Enddatum mindestens das Enddatum |
| `resolution_state` | aus dem Status: `Active` → `New`, `Resolved` → `Closing` |
| `additional_info` | `properties.description` als Klartext (ohne HTML), gekürzt auf `MAX_TEXT_LENGTH` |

`message_key` ist bei Meldung und Schließen gleich, damit ServiceNow denselben Alert aktualisiert bzw. schließt.
Achtung: Der Schlüssel enthält keine Subscription. Erzeugt dasselbe Azure-Event mehrere Knoten, haben alle denselben
`message_key`. Ob ServiceNow daraus einen Alert oder einen pro Knoten macht, hängt von den Event-Regeln ab.

### Severity-Mapping
Die Severity kommt aus dem Azure-Feld `properties.level`. Der Event-Typ spielt dafür keine Rolle.
Die Tabelle steht als Konstante `_SEVERITY` in `servicehealth_sync/event_mapper.py` und lässt sich dort ohne Logikänderung anpassen.

| Azure `level` | ServiceNow `severity` | Bedeutung in ServiceNow |
|---|---|---|
| `Critical` | `1` | Critical |
| `Error` | `2` | Major (liefert die API normalerweise nicht) |
| `Warning` | `3` | Minor |
| `Informational` | `5` | OK |
| `Verbose` | `5` | OK |
| fehlt oder unbekannt | `5` | OK |
| **geschlossen** (siehe Status) | `0` | Clear, unabhängig vom Level |

Die Stufe `4` (Warning) wird derzeit nicht vergeben, weil Azure keinen passenden Level liefert.

### Status und Schließen
Der Status folgt Microsoft: `Active` → `resolution_state = New`, `Resolved` → `Closing` mit `severity = 0`.

Ausnahme **Health Advisories** (`eventType = HealthAdvisory`): Microsoft setzt sie nicht zuverlässig auf `Resolved`.
Ist ihr Enddatum (`impactMitigationTime`) erreicht, wird der Alert deshalb trotzdem mit `Closing` und `severity = 0`
geschlossen. Offene Advisories mit Enddatum in der Zukunft merkt sich der Job in `state.json` und schließt sie zum
Enddatum, auch wenn Azure sie nicht mehr liefert. Ein Platzhalter-Datum (`0001-01-01`) wird ignoriert.
Alle anderen Event-Typen (z. B. `ServiceIssue`) werden nur über Microsofts Status geschlossen.

## Konfiguration (Umgebungsvariablen / GitLab CI/CD-Variablen, Secrets als *masked*)
| Variable | Bedeutung |
|---|---|
| `AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET` | Service Principal mit Rolle *Reader* |
| `AZURE_SUBSCRIPTION_IDS` | *Optional.* Leer = **alle** Subscriptions, die der Service Principal sehen darf. Gesetzt = nur diese (Komma/Semikolon-getrennt) |
| `AZURE_EXCLUDE_SUBSCRIPTION_IDS` | *Optional.* Subscriptions, die ausgelassen werden |
| `SERVICENOW_INSTANCE` | Host, z. B. `instanz.service-now.com` |
| `SERVICENOW_SOURCE` | Wert für `source=` in der Webhook-URL |
| `SERVICENOW_CONNECTOR_SYS_ID` | Wert für `sys_id=` in der Webhook-URL |
| `SERVICENOW_TOKEN` | API-Key, wird als Header `x-sn-apikey` gesendet |
| `LOOKBACK_DAYS` (30), `OVERLAP_HOURS` (24) | Rückblick beim ersten Lauf, Sicherheits-Überlappung |
| `MAX_TEXT_LENGTH` (3000) | Kürzung von `additional_info` |
| `PRINT_PAYLOAD` (`true`) | `false`: Payloads nicht vor dem Senden im Job-Log ausgeben (für Schedules sinnvoll) |
| `FORCE_RESEND` (`false`) | `true`: Zustand ignorieren und alle Events im Zeitfenster neu senden (nur für einen manuellen Lauf setzen) |
| `DRY_RUN` (`false`) | `true`: Payloads nur ausgeben, nichts senden, Zustand nicht ändern |
| `HTTPS_PROXY`, `NO_PROXY` | Proxy, wird von `requests` automatisch beachtet |

`MAX_REGIONS` und `ALERT_RULE_NAME` werden noch gelesen, haben aber keine Wirkung mehr.

## Events erneut senden
Mit `FORCE_RESEND=true` bei *Run pipeline* (CI/CD → Pipelines). Alternativ das Artifact des letzten erfolgreichen
Jobs löschen, dann startet der nächste Lauf ohne Zustand. Ob ein erneutes `New` für einen schon geschlossenen Alert
diesen wieder öffnet, hängt von den Event-Regeln in ServiceNow ab.

## Lokal testen
```bash
pip install -r requirements.txt
python -m unittest discover -s tests -t .
# Trockenlauf gegen echte Azure-Daten (sendet nichts an ServiceNow):
export AZURE_TENANT_ID=... AZURE_CLIENT_ID=... AZURE_CLIENT_SECRET=...
export SERVICENOW_INSTANCE=x SERVICENOW_SOURCE=x SERVICENOW_CONNECTOR_SYS_ID=x SERVICENOW_TOKEN=x
python -m servicehealth_sync --dry-run
```
