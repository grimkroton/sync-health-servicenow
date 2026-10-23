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
            status_forcelist=(429, 502, 503, 504),  # 500 = Validierungsfehler, nicht wiederholen
            allowed_methods=frozenset({"POST"}),
            respect_retry_after_header=True,
            raise_on_status=False,
        )
        self.session = requests.Session()
        self.session.mount("https://", HTTPAdapter(max_retries=retry))
        self.session.mount("http://", HTTPAdapter(max_retries=retry))
        # Authentifizierung per API-Key im Header x-sn-apikey (kein Bearer Token).
        # Proxy-Einstellungen (HTTPS_PROXY / NO_PROXY) liest requests selbst aus der Umgebung.
        self.session.headers["x-sn-apikey"] = token

    def send(self, payload: dict) -> None:
        """Sendet ein Event. Wirft ServiceNowError, wenn es nicht angenommen wurde."""
        self._post(json=payload)

    def send_raw(self, body: bytes, content_type: str | None) -> None:
        """Sendet einen fertigen Body mit frei wählbarem Content-Type (nur für die Diagnose)."""
        headers = {"Content-Type": content_type} if content_type else {}
        self._post(data=body, headers=headers)

    def _post(self, **kwargs) -> None:
        try:
            # Keine Weiterleitungen folgen: Ein Redirect (z. B. auf eine Login-Seite) würde als POST->GET
            # mit HTTP 200 enden und sähe wie ein Erfolg aus, ohne dass ein Event angelegt wurde.
            response = self.session.post(
                self.url, params=self.params, timeout=self.timeout, allow_redirects=False, **kwargs
            )
        except requests.RequestException as exc:
            log.error("ServiceNow: keine Antwort (%s)", exc)
            raise ServiceNowError(f"Verbindung zu ServiceNow fehlgeschlagen: {exc}") from exc

        body = response.text.strip().replace("\n", " ")[:300]
        log.info(
            "ServiceNow: HTTP %s %s (Content-Type: %s) Antwort: %s",
            response.status_code, response.reason or "", response.headers.get("Content-Type", "-"), body or "<leer>",
        )
        if 300 <= response.status_code < 400:
            raise ServiceNowError(
                f"ServiceNow leitet um (HTTP {response.status_code}) auf {response.headers.get('Location', '?')}: "
                "Event wurde nicht angenommen"
            )
        if not response.ok:
            raise ServiceNowError(f"ServiceNow antwortete mit HTTP {response.status_code}: {body}")
