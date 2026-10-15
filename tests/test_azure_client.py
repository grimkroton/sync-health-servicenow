import unittest
from datetime import datetime, timezone

from servicehealth_sync.azure_client import AzureClient, AzureError
from tests.helpers import FakeServer

SUB = "def04582-9246-4743-9083-8544b0340c23"


class AzureClientTests(unittest.TestCase):
    def start(self, route):
        self.srv = FakeServer(route)
        self.addCleanup(self.srv.close)
        return AzureClient(
            "tenant-1", "client-1", "secret-1", timeout=5,
            login_base=self.srv.base, management_base=self.srv.base, retries=1, backoff_factor=0,
        )

    def test_token_and_paging(self):
        def route(req, n):
            if req["path"].endswith("/oauth2/v2.0/token"):
                return 200, {"access_token": "tok", "expires_in": 3600}
            if "page2" in req["query"]:
                return 200, {"value": [{"name": "B"}]}
            return 200, {"value": [{"name": "A"}], "nextLink": f"{self.srv.base}/next?page2=1"}

        client = self.start(route)
        events = list(client.list_events(SUB, datetime(2026, 9, 5, tzinfo=timezone.utc)))
        self.assertEqual([e["name"] for e in events], ["A", "B"])

        token_req = self.srv.received[0]
        self.assertEqual(token_req["path"], "/tenant-1/oauth2/v2.0/token")
        body = token_req["raw"].decode()
        self.assertIn("grant_type=client_credentials", body)
        self.assertIn("scope=https%3A%2F%2Fmanagement.azure.com%2F.default", body)

        first = self.srv.received[1]
        self.assertEqual(first["path"], f"/subscriptions/{SUB}/providers/Microsoft.ResourceHealth/events")
        self.assertEqual(first["query"]["api-version"], "2024-02-01")
        self.assertEqual(first["query"]["queryStartTime"], "9/5/2026")
        self.assertEqual(first["headers"]["authorization"], "Bearer tok")
        # Token wird wiederverwendet: genau ein Token-Request
        self.assertEqual(sum(r["path"].endswith("/token") for r in self.srv.received), 1)

    def test_token_error_contains_reason_but_not_secret(self):
        def route(req, n):
            return 401, {"error": "invalid_client", "error_description": "AADSTS7000215: Invalid client secret provided."}

        client = self.start(route)
        with self.assertRaises(AzureError) as ctx:
            list(client.list_events(SUB, datetime(2026, 9, 5, tzinfo=timezone.utc)))
        self.assertIn("AADSTS7000215", str(ctx.exception))
        self.assertNotIn("secret-1", str(ctx.exception))

    def test_forbidden_raises(self):
        def route(req, n):
            if req["path"].endswith("/token"):
                return 200, {"access_token": "tok", "expires_in": 3600}
            return 403, {"error": {"code": "AuthorizationFailed"}}

        client = self.start(route)
        with self.assertRaises(AzureError) as ctx:
            list(client.list_events(SUB, datetime(2026, 9, 5, tzinfo=timezone.utc)))
        self.assertIn("403", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
