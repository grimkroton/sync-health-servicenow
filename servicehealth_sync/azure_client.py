"""Abruf der Service Health Events über die Azure Resource Health REST API."""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any, Iterator

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

log = logging.getLogger(__name__)

API_VERSION = "2024-02-01"
SCOPE = "https://management.azure.com/.default"


class AzureError(Exception):
    """Token- oder Event-Abruf bei Azure ist fehlgeschlagen."""


def _query_start(value: datetime) -> str:
    # Das Beispiel der Microsoft-Doku nutzt das Format M/D/YYYY (z. B. 7/24/2020).
    return f"{value.month}/{value.day}/{value.year}"


class AzureClient:
    def __init__(
        self,
        tenant_id: str,
        client_id: str,
        client_secret: str,
        timeout: int = 30,
        login_base: str = "https://login.microsoftonline.com",
        management_base: str = "https://management.azure.com",
        retries: int = 4,
        backoff_factor: float = 2.0,
    ) -> None:
        self.token_url = f"{login_base}/{tenant_id}/oauth2/v2.0/token"
        self.management_base = management_base
        self.client_id = client_id
        self.client_secret = client_secret
        self.timeout = timeout
        self._token: str | None = None
        self._token_expires = 0.0

        retry = Retry(
            total=retries,
            backoff_factor=backoff_factor,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET", "POST"}),
            respect_retry_after_header=True,
            raise_on_status=False,
        )
        self.session = requests.Session()
        self.session.mount("https://", HTTPAdapter(max_retries=retry))
        self.session.mount("http://", HTTPAdapter(max_retries=retry))

    def _get_token(self) -> str:
        if self._token and time.time() < self._token_expires:
            return self._token
        try:
            response = self.session.post(
                self.token_url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "scope": SCOPE,
                },
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise AzureError(f"Token-Abruf fehlgeschlagen: {exc}") from exc

        try:
            data = response.json()
        except ValueError:
            data = {}
        if not response.ok or "access_token" not in data:
            # Die AADSTS-Nummer steht in error_description; das Secret wird nie geloggt.
            reason = str(data.get("error_description") or data.get("error") or response.text)[:300]
            raise AzureError(f"Token-Abruf fehlgeschlagen (HTTP {response.status_code}): {reason}")
        self._token = data["access_token"]
        self._token_expires = time.time() + max(int(data.get("expires_in", 3600)) - 120, 60)
        return self._token

    def list_events(self, subscription_id: str, since: datetime) -> Iterator[dict[str, Any]]:
        """Liefert alle Service Health Events der Subscription, seit `since` aktualisiert."""
        url: str | None = (
            f"{self.management_base}/subscriptions/{subscription_id}"
            "/providers/Microsoft.ResourceHealth/events"
        )
        params: dict[str, str] | None = {
            "api-version": API_VERSION,
            "queryStartTime": _query_start(since),
        }
        while url:
            try:
                response = self.session.get(
                    url,
                    params=params,
                    headers={"Authorization": f"Bearer {self._get_token()}"},
                    timeout=self.timeout,
                )
            except requests.RequestException as exc:
                raise AzureError(f"Event-Abruf fehlgeschlagen: {exc}") from exc
            if not response.ok:
                body = response.text.strip().replace("\n", " ")[:300]
                raise AzureError(f"Event-Abruf für {subscription_id} fehlgeschlagen "
                                 f"(HTTP {response.status_code}): {body}")
            data = response.json()
            yield from data.get("value") or []
            url = data.get("nextLink")  # enthält die Query-Parameter bereits
            params = None
