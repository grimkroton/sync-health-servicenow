"""Bildet ein Event der Resource Health Events API auf das ServiceNow-Eventformat ab.

Das Format entspricht dem generischen Event-Management-Webhook: pro Request ein einzelnes Event
(flaches Objekt mit festen Feldern). Pro betroffener Subscription entsteht ein eigenes Event.
"""

from __future__ import annotations

import copy
import html
from datetime import datetime, timezone
from typing import Any

from .mapper import strip_html, truncate

# Diese Werte lassen sich hier zentral anpassen (z. B. passend zu den Event-Regeln in ServiceNow).
EVENT_SOURCE_FALLBACK = "ServiceHealth"  # falls die API keine eventSource liefert
EVENT_CLASS = "Azure Service Health"

# ServiceNow: 0 = Clear, 1 = Critical, 2 = Major, 3 = Minor, 4 = Warning, 5 = OK
# Die Severity kommt aus dem Azure-Level des Events. Ein geschlossenes Event bekommt immer 0.
_SEVERITY = {
    "critical": "1",
    "error": "2",
    "warning": "3",
    "informational": "5",
    "verbose": "5",
}
_SEVERITY_DEFAULT = "5"


def parse_time(value: str | None) -> datetime | None:
    """ISO-8601 -> UTC-datetime. Platzhalter wie 0001-01-01 und Unlesbares ergeben None."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    if parsed.year < 2000:
        return None
    return parsed.astimezone(timezone.utc)


def snow_time(value: str | None) -> str:
    """ISO-8601 (UTC) -> 'YYYY-MM-DD HH:MM:SS', das Zeitformat von time_of_event."""
    if not value:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    parsed = parse_time(value)
    if parsed is None:
        return value[:19].replace("T", " ")
    return parsed.strftime("%Y-%m-%d %H:%M:%S")


def advisory_expiry(props: dict[str, Any]) -> datetime | None:
    """Enddatum eines noch offenen Health Advisory; für alles andere None.

    Microsoft setzt Advisories nicht zuverlässig auf Resolved. Ist das Enddatum
    (impactMitigationTime) erreicht, gilt ein Advisory deshalb als beendet.
    """
    if (props.get("status") or "").lower() == "resolved":
        return None
    if props.get("eventType") != "HealthAdvisory":
        return None
    return parse_time(props.get("impactMitigationTime"))


def effective_status(props: dict[str, Any], now: datetime) -> str:
    """Status von Microsoft; bei Advisories mit erreichtem Enddatum 'Resolved'."""
    status = props.get("status") or "Active"
    if status.lower() == "resolved":
        return "Resolved"
    expiry = advisory_expiry(props)
    if expiry is not None and expiry <= now:
        return "Resolved"
    return status


def _clean_title(value: str | None) -> str:
    text = html.unescape(value or "").replace("\xa0", " ")
    return " ".join(text.split())


def impacted_nodes(props: dict[str, Any], fallback_subscription: str) -> dict[str, list[str]]:
    """Betroffene Subscriptions -> betroffene Services (jeweils in der Reihenfolge der API).

    Nennt die API keine Subscription, wird die abgefragte Subscription als Knoten verwendet.
    """
    nodes: dict[str, list[str]] = {}
    for item in props.get("impact") or []:
        service = item.get("impactedService") or ""
        for region in item.get("impactedRegions") or []:
            for sub in region.get("impactedSubscriptions") or []:
                services = nodes.setdefault(sub, [])
                if service and service not in services:
                    services.append(service)
    if not nodes:
        services: list[str] = []
        for item in props.get("impact") or []:
            service = item.get("impactedService") or ""
            if service and service not in services:
                services.append(service)
        nodes[fallback_subscription] = services
    return nodes


def map_event(
    event: dict[str, Any],
    subscription_id: str,
    *,
    max_text_length: int = 3000,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Ein Azure-Event -> eine Liste von ServiceNow-Events, je betroffener Subscription eines."""
    now = now or datetime.now(timezone.utc)
    props = event.get("properties") or {}
    tracking_id = event.get("name") or ""
    title = _clean_title(props.get("title"))
    level = props.get("level") or props.get("eventLevel") or "Informational"
    status = effective_status(props, now)
    resolved = status.lower() == "resolved"
    expiry = advisory_expiry(props)
    closed_by_expiry = resolved and expiry is not None
    details = truncate(strip_html(props.get("description") or props.get("summary")), max_text_length)
    last_update = props.get("lastUpdateTime") or props.get("impactStartTime")

    event_time = snow_time(last_update)
    if closed_by_expiry:
        event_time = max(event_time, snow_time(expiry.isoformat()))

    # message_key = id des Events, gleich bei Fired und Resolved: ServiceNow aktualisiert bzw. schließt damit den Alert.
    message_key = event.get("id") or f"Microsoft.ResourceHealth/events/{tracking_id}"

    records = []
    for node, services in impacted_nodes(props, subscription_id).items():
        records.append({
            "source": props.get("eventSource") or EVENT_SOURCE_FALLBACK,
            "event_class": EVENT_CLASS,
            "node": node,
            "resource": ", ".join(services),
            "metric_name": "",
            "type": props.get("eventType") or "",
            "severity": "0" if resolved else _SEVERITY.get(str(level).lower(), _SEVERITY_DEFAULT),
            "description": title,
            "message_key": message_key,
            "time_of_event": event_time,
            "resolution_state": "Closing" if resolved else "New",
            "additional_info": details,
        })
    return records


def closing_payload(payload: dict[str, Any], at: str) -> dict[str, Any]:
    """Schließt einen früher gesendeten Alert, ohne das Event erneut bei Azure abzufragen."""
    record = copy.deepcopy(payload)
    record["severity"] = "0"
    record["resolution_state"] = "Closing"
    record["time_of_event"] = max(record["time_of_event"], snow_time(at))
    return record
