"""Ablauf: Events abrufen, mit dem Zustand vergleichen, mappen und an ServiceNow senden."""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone

from . import state as state_mod
from .azure_client import AzureClient, AzureError
from .config import Config, ConfigError
from .event_mapper import advisory_expiry, closing_payload, effective_status, map_event
from .servicenow_client import ServiceNowClient, ServiceNowError

log = logging.getLogger("servicehealth_sync")


# Subscriptions in diesen Zuständen liefern keine Events mehr
_INACTIVE_STATES = {"disabled", "deleted", "expired"}


def _print_payload_enabled() -> bool:
    """Ausgabe des Payloads vor dem Senden; abschaltbar mit PRINT_PAYLOAD=false."""
    return os.environ.get("PRINT_PAYLOAD", "true").strip().lower() not in ("0", "false", "no", "nein", "off")


def _force_resend_enabled() -> bool:
    """FORCE_RESEND=true: Zustand ignorieren und alle Events im Zeitfenster neu senden."""
    return os.environ.get("FORCE_RESEND", "false").strip().lower() in ("1", "true", "yes", "ja", "on")


def load_state(cfg: Config) -> state_mod.State:
    """Zustand aus Datei oder dem letzten GitLab-Lauf; bei FORCE_RESEND ein leerer Zustand."""
    if _force_resend_enabled():
        log.warning("FORCE_RESEND ist aktiv: Zustand wird ignoriert, alle Events im Zeitfenster werden neu gesendet")
        return state_mod.State()
    return (
        state_mod.load(cfg.state_file)
        or state_mod.fetch_from_gitlab(cfg.state_file, cfg.request_timeout)
        or state_mod.State()
    )


def select_subscriptions(discovered: list[dict[str, str]], cfg: Config) -> list[dict[str, str]]:
    """Alle sichtbaren, aktiven Subscriptions; optional eingeschränkt bzw. um Ausschlüsse bereinigt."""
    allow = {x.lower() for x in cfg.subscription_ids}
    deny = {x.lower() for x in cfg.exclude_subscription_ids}
    visible = {d["subscriptionId"].lower() for d in discovered}
    for missing in sorted(allow - visible):
        log.warning("Konfigurierte Subscription %s… ist für den Service Principal nicht sichtbar", missing[:8])

    selected = []
    for sub in discovered:
        sid = sub["subscriptionId"].lower()
        if not sid or sub.get("state", "").lower() in _INACTIVE_STATES:
            continue
        if allow and sid not in allow:
            continue
        if sid in deny:
            continue
        selected.append(sub)
    return selected


def run(cfg: Config, azure, servicenow, state: state_mod.State, now: datetime | None = None) -> int:
    """Führt einen Lauf aus. Rückgabe: Anzahl der Fehler (0 = alles gut)."""
    now = now or datetime.now(timezone.utc)
    lookback_since = now - timedelta(days=cfg.lookback_days)
    normal_since = state.last_run - timedelta(hours=cfg.overlap_hours) if state.last_run else lookback_since

    try:
        subscriptions = select_subscriptions(azure.list_subscriptions(), cfg)
    except AzureError as exc:
        log.error("%s", exc)
        return 1
    if not subscriptions:
        log.error("Keine Subscription gefunden: Hat der Service Principal die Rolle Reader?")
        return 1
    log.info("%d Subscription(s) werden abgefragt", len(subscriptions))

    # Zustandsdateien ohne Ablauf-Verfolgung: einmal das volle Zeitfenster prüfen und die Enddaten erfassen
    full_scan = not state.expiry_tracking
    errors = sent = skipped = 0
    for subscription in subscriptions:
        sub = subscription["subscriptionId"]
        label = f"{subscription.get('displayName') or sub} ({sub[:8]}…)"
        # Neue Subscriptions bekommen den vollen Rückblick, damit schon aktive Events nicht verpasst werden.
        since = normal_since if state.is_known_subscription(sub) and not full_scan else lookback_since
        sub_errors = 0
        try:
            events = list(azure.list_events(sub, since))
        except AzureError as exc:
            log.error("%s: %s", label, exc)
            errors += 1
            continue
        log.info("%s: %d Event(s) geliefert (seit %s)", label, len(events), since.date())

        for event in events:
            props = event.get("properties") or {}
            tracking_id = event.get("name") or ""
            status = effective_status(props, now)  # Microsofts Status; Advisories mit erreichtem Enddatum: Resolved
            last_update = props.get("lastUpdateTime") or ""
            if not tracking_id:
                log.warning("Event ohne Namen übersprungen: %s", event.get("id"))
                continue

            expiry = advisory_expiry(props)
            pending_expiry = expiry if expiry is not None and expiry > now else None
            # Pro betroffener Subscription (node) ein eigenes Event
            for payload in map_event(event, sub, max_text_length=cfg.max_text_length, now=now):
                node = payload["node"]
                key = state_mod.event_key(node, tracking_id)
                if not state.needs_send(key, status, last_update):
                    skipped += 1
                    if pending_expiry and not cfg.dry_run:
                        state.track_expiry(key, pending_expiry, last_update, payload)
                    continue
                if cfg.dry_run:
                    log.info("DRY_RUN: %s (%s) – Knoten %s", tracking_id, status, node)
                    print(json.dumps(payload, indent=2, ensure_ascii=False))
                    continue
                if _print_payload_enabled():
                    log.info("Payload für %s (%s), Knoten %s – wird jetzt gesendet:", tracking_id, status, node)
                    print(json.dumps(payload, indent=2, ensure_ascii=False), flush=True)
                try:
                    servicenow.send(payload)
                except ServiceNowError as exc:
                    log.error("Senden von %s (Knoten %s) fehlgeschlagen: %s", tracking_id, node, exc)
                    sub_errors += 1
                    continue
                state.mark_sent(key, status, last_update)
                if pending_expiry:
                    state.track_expiry(key, pending_expiry, last_update, payload)
                else:
                    state.clear_expiry(key)
                sent += 1
                log.info("Gesendet: %s (%s), Knoten %s – %s", tracking_id, status, node, props.get("title", ""))

        errors += sub_errors
        if sub_errors == 0 and not cfg.dry_run:
            state.mark_subscription(sub)

    # Offene Advisories, deren Enddatum erreicht ist, ohne dass Azure sie erneut geliefert hat
    for key, entry in state.due_expiries(now):
        tracking_id = key.split("|", 1)[-1]
        payload = closing_payload(entry["payload"], entry["at"])
        if cfg.dry_run:
            log.info("DRY_RUN: %s wird wegen erreichtem Enddatum geschlossen", tracking_id)
            print(json.dumps(payload, indent=2, ensure_ascii=False))
            continue
        if _print_payload_enabled():
            log.info("Payload für %s (Resolved, Enddatum erreicht) – wird jetzt gesendet:", tracking_id)
            print(json.dumps(payload, indent=2, ensure_ascii=False), flush=True)
        try:
            servicenow.send(payload)
        except ServiceNowError as exc:
            log.error("Schließen von %s fehlgeschlagen: %s", tracking_id, exc)
            errors += 1
            continue
        state.mark_sent(key, "Resolved", entry.get("last_update", ""))
        state.clear_expiry(key)
        sent += 1
        log.info("Geschlossen (Enddatum erreicht): %s", tracking_id)

    log.info("Fertig: %d gesendet, %d unverändert, %d Fehler", sent, skipped, errors)
    if errors == 0 and not cfg.dry_run:
        state.expiry_tracking = True
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

    state = load_state(cfg)
    azure = AzureClient(cfg.tenant_id, cfg.client_id, cfg.client_secret, cfg.request_timeout)
    servicenow = ServiceNowClient(
        cfg.sn_instance, cfg.sn_source, cfg.sn_connector_sys_id, cfg.sn_token, cfg.request_timeout
    )

    errors = run(cfg, azure, servicenow, state)
    if not cfg.dry_run:
        state_mod.save(state, cfg.state_file)
    return 1 if errors else 0
