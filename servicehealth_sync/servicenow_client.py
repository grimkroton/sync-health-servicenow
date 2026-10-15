"""Push von Events an den generischen Webhook des ServiceNow Event Management."""

from __future__ import annotations

import logging

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

log = logging.getLogger(__name__)

WEBHOOK_PATH = "/api/sn_em_connector/em/inbound_event"


class ServiceNowError(Exception):
    """Der Webhook hat das Event nicht angenommen."""


class ServiceNowClient:
    def __init__(
        self,
        instance: str,
        source: str,
        connector_sys_id: str,
        token: str,
        timeout: int = 30,
        scheme: str = "https",
        retries: int = 4,
        backoff_factor: float = 2.0,
    ) -> None:
        self.url = f"{scheme}://{instance}{WEBHOOK_PATH}"
        self.params = {"source": source, "sys_id": connector_sys_id, "custom": "true"}
        self.timeout = timeout

        retry = Retry(
            total=retries,
            backoff_factor=backoff_factor,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"POST"}),
            respect_retry_after_header=True,
            raise_on_status=False,
        )
        self.session = requests.Session()
        self.session.mount("https://", HTTPAdapter(max_retries=retry))
        self.session.mount("http://", HTTPAdapter(max_retries=retry))
        # Authentifizierung per Bearer Token.
        # Proxy-Einstellungen (HTTPS_PROXY / NO_PROXY) liest requests selbst aus der Umgebung.
        self.session.headers["Authorization"] = f"Bearer {token}"

    def send(self, payload: dict) -> None:
        """Sendet ein Event. Wirft ServiceNowError, wenn es nicht angenommen wurde."""
        try:
            response = self.session.post(
                self.url, params=self.params, json=payload, timeout=self.timeout
            )
        except requests.RequestException as exc:
            raise ServiceNowError(f"Verbindung zu ServiceNow fehlgeschlagen: {exc}") from exc

        if not response.ok:
            body = response.text.strip().replace("\n", " ")[:300]
            raise ServiceNowError(f"ServiceNow antwortete mit HTTP {response.status_code}: {body}")
        log.debug("ServiceNow hat das Event angenommen (HTTP %s)", response.status_code)
