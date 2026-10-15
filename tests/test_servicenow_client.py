import json
import unittest

from servicehealth_sync.servicenow_client import ServiceNowClient, ServiceNowError
from tests.helpers import FakeServer, unused_port


class ServiceNowClientTests(unittest.TestCase):
    def start(self, statuses):
        queue = list(statuses)

        def route(req, n):
            status = queue.pop(0) if queue else 200
            return status, ({"result": "ok"} if status < 400 else {"error": "nope"})

        self.srv = FakeServer(route)
        self.addCleanup(self.srv.close)

    def client(self, retries=4, instance=None):
        return ServiceNowClient(
            instance=instance or f"127.0.0.1:{self.srv.port}", source="quelle",
            connector_sys_id="abc123", token="geheim", scheme="http",
            retries=retries, backoff_factor=0,
        )

    def test_sends_json_with_bearer_token_and_query(self):
        self.start([])
        self.client().send({"hallo": "welt"})
        req = self.srv.received[0]
        self.assertEqual(req["path"], "/api/sn_em_connector/em/inbound_event")
        self.assertEqual(req["query"], {"source": "quelle", "sys_id": "abc123", "custom": "true"})
        self.assertEqual(req["headers"]["authorization"], "Bearer geheim")
        self.assertEqual(json.loads(req["raw"]), {"hallo": "welt"})

    def test_retries_on_503_then_succeeds(self):
        self.start([503, 503, 200])
        self.client(retries=3).send({"a": 1})
        self.assertEqual(len(self.srv.received), 3)

    def test_gives_up_after_retries(self):
        self.start([503] * 10)
        with self.assertRaises(ServiceNowError) as ctx:
            self.client(retries=2).send({"a": 1})
        self.assertIn("503", str(ctx.exception))
        self.assertEqual(len(self.srv.received), 3)  # 1 Versuch + 2 Wiederholungen

    def test_client_error_is_not_retried(self):
        self.start([401])
        with self.assertRaises(ServiceNowError) as ctx:
            self.client().send({"a": 1})
        self.assertIn("401", str(ctx.exception))
        self.assertEqual(len(self.srv.received), 1)

    def test_connection_error_raises_servicenow_error(self):
        self.start([])
        with self.assertRaises(ServiceNowError):
            self.client(retries=0, instance=f"127.0.0.1:{unused_port()}").send({"a": 1})


if __name__ == "__main__":
    unittest.main()
