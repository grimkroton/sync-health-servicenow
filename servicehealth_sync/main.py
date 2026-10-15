"""Ablauf: Events abrufen, mit dem Zustand vergleichen, mappen und an ServiceNow senden."""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timedelta, timezone

from . import state as state_mod
from .azure_client import AzureClient, AzureError
from .config import Config, ConfigError
from .mapper import map_event
from .servicenow_client import ServiceNowClient, ServiceNowError

log = logging.getLogger("servicehealth_sync")


def run(cfg: Config, azure, servicenow, state: state_mod.State, now: datetime | None = None) -> int:
    """Führt einen Lauf aus. Rückgabe: Anzahl der Fehler (0 = alles gut)."""
    now = now or datetime.now(timezone.utc)
    if state.last_run:
        since = state.last_run - timedelta(hours=cfg.overlap_hours)
    else:
        since = now - timedelta(days=cfg.lookback_days)
    log.info("Rufe Events ab, aktualisiert seit %s (%d Subscription(s))", since.date(), len(cfg.subscription_ids))

    errors = sent = skipped = 0
    for sub in cfg.subscription_ids:
        try:
            events = list(azure.list_events(sub, since))
        except AzureError as exc:
            log.error("%s", exc)
            errors += 1
            continue
        log.info("Subscription %s…: %d Event(s) geliefert", sub[:8], len(events))

        for event in events:
            props = event.get("properties") or {}
            tracking_id = event.get("name") or ""
            status = props.get("status") or "Active"
            last_update = props.get("lastUpdateTime") or ""
            key = state_mod.event_key(sub, tracking_id)
            if not tracking_id:
                log.warning("Event ohne Namen übersprungen: %s", event.get("id"))
                continue
            if not state.needs_send(key, status, last_update):
                skipped += 1
                continue

            payload = map_event(
                event, sub,
                alert_rule=cfg.alert_rule_name,
                max_text_length=cfg.max_text_length,
                max_regions=cfg.max_regions,
            )
            if cfg.dry_run:
                log.info("DRY_RUN: %s (%s)", tracking_id, status)
                print(json.dumps(payload, indent=2, ensure_ascii=False))
                continue
            try:
                servicenow.send(payload)
            except ServiceNowError as exc:
                log.error("Senden von %s fehlgeschlagen: %s", tracking_id, exc)
                errors += 1
                continue
            state.mark_sent(key, status, last_update)
            sent += 1
            log.info("Gesendet: %s (%s) – %s", tracking_id, status, props.get("title", ""))

    log.info("Fertig: %d gesendet, %d unverändert, %d Fehler", sent, skipped, errors)
    if errors == 0 and not cfg.dry_run:
        state.last_run = now  # nur bei fehlerfreiem Lauf vorrücken, sonst wird Verpasstes nachgeholt
    return errors


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        cfg = Config.from_env()
    except ConfigError as exc:
        log.error("%s", exc)
        return 2
    if "--dry-run" in sys.argv:
        cfg = Config(**{**cfg.__dict__, "dry_run": True})

    state = (
        state_mod.load(cfg.state_file)
        or state_mod.fetch_from_gitlab(cfg.state_file, cfg.request_timeout)
        or state_mod.State()
    )
    azure = AzureClient(cfg.tenant_id, cfg.client_id, cfg.client_secret, cfg.request_timeout)
    servicenow = ServiceNowClient(
        cfg.sn_instance, cfg.sn_source, cfg.sn_connector_sys_id, cfg.sn_token, cfg.request_timeout
    )
    errors = run(cfg, azure, servicenow, state)
    if not cfg.dry_run:
        state_mod.save(state, cfg.state_file)
    return 1 if errors else 0
