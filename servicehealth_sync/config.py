"""Konfiguration aus Umgebungsvariablen (in GitLab als CI/CD-Variablen hinterlegt)."""

from __future__ import annotations

import os
from dataclasses import dataclass


class ConfigError(Exception):
    """Pflichtvariable fehlt oder hat einen ungültigen Wert."""


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(f"Umgebungsvariable {name} fehlt oder ist leer")
    return value


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} muss eine Ganzzahl sein, ist aber {raw!r}") from exc


def _bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "ja", "on"}


def _id_list(name: str) -> tuple[str, ...]:
    raw = os.environ.get(name, "")
    return tuple(x.strip() for x in raw.replace(";", ",").split(",") if x.strip())


def _hostname(value: str) -> str:
    """Akzeptiert 'instanz.service-now.com' ebenso wie 'https://instanz.service-now.com/'."""
    value = value.strip()
    for prefix in ("https://", "http://"):
        if value.lower().startswith(prefix):
            value = value[len(prefix):]
    return value.rstrip("/")


@dataclass(frozen=True)
class Config:
    # Azure
    tenant_id: str
    client_id: str
    client_secret: str
    # ServiceNow (Authentifizierung per API-Key im Header x-sn-apikey)
    sn_instance: str
    sn_source: str
    sn_connector_sys_id: str
    sn_token: str
    # Verhalten
    lookback_days: int = 30
    overlap_hours: int = 24
    max_text_length: int = 3000
    max_regions: int = 10
    request_timeout: int = 30
    alert_rule_name: str = "ServiceHealth-Pull"
    dry_run: bool = False
    state_file: str = "state.json"
    # Optional: leer = alle Subscriptions, die der Service Principal sehen darf
    subscription_ids: tuple[str, ...] = ()
    exclude_subscription_ids: tuple[str, ...] = ()

    @classmethod
    def from_env(cls) -> "Config":
        dry_run = _bool("DRY_RUN")
        return cls(
            tenant_id=_required("AZURE_TENANT_ID"),
            client_id=_required("AZURE_CLIENT_ID"),
            client_secret=_required("AZURE_CLIENT_SECRET"),
            sn_instance=_hostname(_required("SERVICENOW_INSTANCE")),
            sn_source=_required("SERVICENOW_SOURCE"),
            sn_connector_sys_id=_required("SERVICENOW_CONNECTOR_SYS_ID"),
            sn_token=_required("SERVICENOW_TOKEN"),
            lookback_days=_int("LOOKBACK_DAYS", 30),
            overlap_hours=_int("OVERLAP_HOURS", 24),
            max_text_length=_int("MAX_TEXT_LENGTH", 3000),
            max_regions=_int("MAX_REGIONS", 10),
            request_timeout=_int("REQUEST_TIMEOUT", 30),
            alert_rule_name=os.environ.get("ALERT_RULE_NAME", "").strip() or "ServiceHealth-Pull",
            dry_run=dry_run,
            state_file=os.environ.get("STATE_FILE", "").strip() or "state.json",
            subscription_ids=_id_list("AZURE_SUBSCRIPTION_IDS"),
            exclude_subscription_ids=_id_list("AZURE_EXCLUDE_SUBSCRIPTION_IDS"),
        )
