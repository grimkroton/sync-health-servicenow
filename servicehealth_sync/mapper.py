"""Bildet ein Event der Resource Health Events API auf das Common Alert Schema ab."""

from __future__ import annotations

from html.parser import HTMLParser
from typing import Any, Iterable

SCHEMA_ID = "azureMonitorCommonAlertSchema"

# Azure Monitor: Sev0 = Critical ... Sev4 = Verbose
_SEVERITY = {
    "critical": "Sev0",
    "error": "Sev1",
    "warning": "Sev2",
    "informational": "Sev3",
    "verbose": "Sev4",
}

_BLOCK_TAGS = {"p", "div", "br", "ul", "ol", "tr", "h1", "h2", "h3", "h4", "h5", "h6"}


class _TextExtractor(HTMLParser):
    """Wandelt HTML in lesbaren Klartext: Absätze, Aufzählungen und Link-Ziele bleiben erhalten."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._href: str | None = None
        self._link_start = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "li":
            self.parts.append("\n- ")
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")
        elif tag == "a":
            self._href = dict(attrs).get("href")
            self._link_start = len(self.parts)

    def handle_endtag(self, tag: str) -> None:
        if tag in _BLOCK_TAGS or tag == "li":
            self.parts.append("\n")
        elif tag == "a" and self._href:
            link_text = "".join(self.parts[self._link_start:]).strip()
            if self._href.strip() != link_text:
                self.parts.append(f" ({self._href.strip()})")
            self._href = None

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def strip_html(html: str | None) -> str:
    if not html:
        return ""
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    text = "".join(parser.parts).replace("\xa0", " ")
    lines = [" ".join(line.split()) for line in text.splitlines()]
    cleaned: list[str] = []
    for line in lines:
        if line or (cleaned and cleaned[-1]):  # höchstens eine Leerzeile am Stück
            cleaned.append(line)
    return "\n".join(cleaned).strip()


def truncate(text: str, limit: int) -> str:
    if limit <= 0 or len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def incident_type(event_type: str | None, title: str | None) -> str:
    """eventType der Events API auf properties.incidentType der Alerts abbilden."""
    mapping = {
        "ServiceIssue": "Incident",
        "PlannedMaintenance": "Maintenance",
        "SecurityAdvisory": "Security",
    }
    if event_type in mapping:
        return mapping[event_type]
    if event_type == "HealthAdvisory":
        # Die API unterscheidet nicht zwischen ActionRequired und Informational.
        if (title or "").strip().lower().startswith("action required"):
            return "ActionRequired"
        return "Informational"
    return event_type or "Informational"


def _unique(values: Iterable[str | None]) -> list[str]:
    seen: dict[str, None] = {}
    for value in values:
        if value and value not in seen:
            seen[value] = None
    return list(seen)


def region_summary(regions: list[str], limit: int) -> str:
    if limit <= 0 or len(regions) <= limit:
        return ", ".join(regions)
    rest = len(regions) - limit
    return f"{', '.join(regions[:limit])} (+{rest} weitere)"


def _impacted(props: dict[str, Any], subscription_id: str) -> tuple[list[str], list[str]]:
    """Betroffene Services und Regionen; Regionen nur, soweit sie diese Subscription betreffen."""
    services: list[str] = []
    regions: list[str] = []
    for item in props.get("impact") or []:
        services.append(item.get("impactedService"))
        for region in item.get("impactedRegions") or []:
            subs = [s.lower() for s in region.get("impactedSubscriptions") or []]
            if subs and subscription_id.lower() not in subs:
                continue
            regions.append(region.get("impactedRegion"))
    return _unique(services), _unique(regions)


def map_event(
    event: dict[str, Any],
    subscription_id: str,
    *,
    alert_rule: str,
    max_text_length: int = 3000,
    max_regions: int = 10,
) -> dict[str, Any]:
    props = event.get("properties") or {}
    tracking_id = event.get("name") or ""
    title = (props.get("title") or "").strip()
    level = props.get("level") or props.get("eventLevel") or "Informational"
    status = props.get("status") or "Active"
    resolved = status.lower() == "resolved"  # der Status folgt Microsoft
    services, regions = _impacted(props, subscription_id)
    content = strip_html(props.get("description") or props.get("summary"))

    essentials: dict[str, Any] = {
        "alertId": f"/subscriptions/{subscription_id}/providers/Microsoft.ResourceHealth/events/{tracking_id}",
        "alertRule": alert_rule,
        "severity": _SEVERITY.get(str(level).lower(), "Sev3"),
        "signalType": "Activity Log",
        "monitorCondition": "Resolved" if resolved else "Fired",
        "monitoringService": "ServiceHealth",
        "alertTargetIDs": [f"/subscriptions/{subscription_id}"],
        "originAlertId": tracking_id,
        "firedDateTime": props.get("impactStartTime"),
        "description": truncate(title, 1000),
        "essentialsVersion": "1.0",
        "alertContextVersion": "1.0",
    }
    if resolved:
        essentials["resolvedDateTime"] = props.get("impactMitigationTime") or props.get("lastUpdateTime")

    return {
        "schemaId": SCHEMA_ID,
        "data": {
            "essentials": essentials,
            "alertContext": {
                "eventSource": "ServiceHealth",
                "level": level,
                "status": status,
                "properties": {
                    "title": title,
                    "service": ", ".join(services),
                    "region": region_summary(regions, max_regions),
                    "incidentType": incident_type(props.get("eventType"), title),
                    "trackingId": tracking_id,
                    "impactStartTime": props.get("impactStartTime"),
                    "impactMitigationTime": props.get("impactMitigationTime"),
                    "stage": status,
                    "defaultLanguageContent": truncate(content, max_text_length),
                },
            },
        },
    }
