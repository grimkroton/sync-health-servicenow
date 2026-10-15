"""Zustand zwischen den Läufen: welche Events wurden in welchem Stand an ServiceNow gesendet."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from typing import Any

import requests

log = logging.getLogger(__name__)


def event_key(subscription_id: str, tracking_id: str) -> str:
    return f"{subscription_id}|{tracking_id}"


class State:
    def __init__(self, data: dict[str, Any] | None = None) -> None:
        data = data or {}
        self.last_run: datetime | None = _parse(data.get("last_run"))
        self.events: dict[str, dict[str, str]] = dict(data.get("events") or {})

    def needs_send(self, key: str, status: str, last_update: str) -> bool:
        """True, wenn das Event neu ist oder sich Status bzw. Aktualisierungszeit geändert haben."""
        known = self.events.get(key)
        return known is None or known.get("status") != status or known.get("lastUpdateTime") != last_update

    def mark_sent(self, key: str, status: str, last_update: str) -> None:
        self.events[key] = {"status": status, "lastUpdateTime": last_update}

    def to_dict(self) -> dict[str, Any]:
        return {
            "last_run": self.last_run.isoformat() if self.last_run else None,
            "events": self.events,
        }


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def load(path: str) -> State | None:
    """Liest eine lokale Zustandsdatei. None, wenn sie fehlt oder unlesbar ist."""
    try:
        with open(path, encoding="utf-8") as fh:
            return State(json.load(fh))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        log.warning("Zustandsdatei %s ist unlesbar (%s), starte ohne Zustand", path, exc)
        return None


def save(state: State, path: str) -> None:
    """Schreibt atomar, damit ein Abbruch keine halbe Datei hinterlässt."""
    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state.to_dict(), fh, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def fetch_from_gitlab(path: str, timeout: int = 30, env: dict[str, str] | None = None) -> State | None:
    """Holt die Zustandsdatei aus dem Artifact des letzten erfolgreichen Laufs dieses Jobs.

    Benötigt die vordefinierten GitLab-CI-Variablen; außerhalb von GitLab oder ohne Artifact
    (erster Lauf, abgelaufen) wird None geliefert.
    """
    env = dict(os.environ if env is None else env)
    api, project, ref, job, token = (
        env.get("CI_API_V4_URL"),
        env.get("CI_PROJECT_ID"),
        env.get("CI_COMMIT_REF_NAME"),
        env.get("CI_JOB_NAME"),
        env.get("CI_JOB_TOKEN"),
    )
    if not all((api, project, ref, job, token)):
        return None
    url = f"{api}/projects/{project}/jobs/artifacts/{requests.utils.quote(ref, safe='')}/raw/{path}"
    try:
        response = requests.get(url, params={"job": job}, headers={"JOB-TOKEN": token}, timeout=timeout)
    except requests.RequestException as exc:
        log.warning("Vorheriger Zustand nicht abrufbar (%s), starte ohne Zustand", exc)
        return None
    if response.status_code == 404:
        log.info("Kein Zustand aus einem früheren Lauf vorhanden")
        return None
    if not response.ok:
        log.warning("Vorheriger Zustand nicht abrufbar (HTTP %s), starte ohne Zustand", response.status_code)
        return None
    try:
        return State(response.json())
    except ValueError:
        log.warning("Vorheriger Zustand ist kein gültiges JSON, starte ohne Zustand")
        return None
