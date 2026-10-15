import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from servicehealth_sync import state as state_mod
from servicehealth_sync.azure_client import AzureError
from servicehealth_sync.config import Config
from servicehealth_sync.main import run
from servicehealth_sync.servicenow_client import ServiceNowError
from tests.helpers import FakeServer
from tests.test_mapper import SUB, make_event

NOW = datetime(2026, 10, 15, 8, 0, tzinfo=timezone.utc)


def cfg(**kw):
    base = dict(
        tenant_id="t", client_id="c", client_secret="s", subscription_ids=(SUB,),
        sn_instance="x", sn_source="q", sn_connector_sys_id="i", sn_token="tok",
    )
    base.update(kw)
    return Config(**base)


class FakeAzure:
    def __init__(self, events=None, error=None):
        self.events, self.error, self.calls = events or [], error, []

    def list_events(self, sub, since):
        self.calls.append((sub, since))
        if self.error:
            raise self.error
        return iter(self.events)


class FakeSN:
    def __init__(self, fail_for=()):
        self.sent, self.fail_for = [], set(fail_for)

    def send(self, payload):
        tid = payload["data"]["essentials"]["originAlertId"]
        if tid in self.fail_for:
            raise ServiceNowError("boom")
        self.sent.append(payload)


def ev(name="ZHXT-XHG", status="Active", last="2026-09-18T16:32:36Z"):
    e = make_event(status=status, lastUpdateTime=last)
    e["name"] = name
    return e


class RunTests(unittest.TestCase):
    def test_first_run_uses_lookback_and_sends_all(self):
        az, sn, st = FakeAzure([ev("A"), ev("B")]), FakeSN(), state_mod.State()
        self.assertEqual(run(cfg(lookback_days=30), az, sn, st, NOW), 0)
        self.assertEqual(len(sn.sent), 2)
        self.assertEqual(az.calls[0][1], NOW - timedelta(days=30))
        self.assertEqual(st.last_run, NOW)

    def test_second_run_sends_only_changes(self):
        st = state_mod.State()
        run(cfg(), FakeAzure([ev("A"), ev("B")]), FakeSN(), st, NOW)
        az, sn = FakeAzure([ev("A"), ev("B", status="Resolved", last="2026-10-01T00:00:00Z")]), FakeSN()
        later = NOW + timedelta(hours=1)
        self.assertEqual(run(cfg(overlap_hours=24), az, sn, st, later), 0)
        self.assertEqual([p["data"]["essentials"]["originAlertId"] for p in sn.sent], ["B"])
        self.assertEqual(sn.sent[0]["data"]["essentials"]["monitorCondition"], "Resolved")
        self.assertEqual(az.calls[0][1], NOW - timedelta(hours=24))

    def test_failed_send_is_retried_next_run_and_last_run_stays(self):
        st = state_mod.State()
        sn = FakeSN(fail_for=["B"])
        self.assertEqual(run(cfg(), FakeAzure([ev("A"), ev("B")]), sn, st, NOW), 1)
        self.assertEqual(len(sn.sent), 1)
        self.assertIsNone(st.last_run)
        sn2 = FakeSN()
        self.assertEqual(run(cfg(), FakeAzure([ev("A"), ev("B")]), sn2, st, NOW), 0)
        self.assertEqual([p["data"]["essentials"]["originAlertId"] for p in sn2.sent], ["B"])

    def test_azure_error_counts_and_continues_with_other_subscriptions(self):
        class Mixed(FakeAzure):
            def list_events(self, sub, since):
                if sub == "bad":
                    raise AzureError("403")
                return iter([ev("A")])

        sn, st = FakeSN(), state_mod.State()
        self.assertEqual(run(cfg(subscription_ids=("bad", SUB)), Mixed(), sn, st, NOW), 1)
        self.assertEqual(len(sn.sent), 1)
        self.assertIsNone(st.last_run)

    def test_dry_run_sends_nothing_and_keeps_state_empty(self):
        sn, st = FakeSN(), state_mod.State()
        self.assertEqual(run(cfg(dry_run=True), FakeAzure([ev("A")]), sn, st, NOW), 0)
        self.assertEqual(sn.sent, [])
        self.assertEqual(st.events, {})
        self.assertIsNone(st.last_run)

    def test_same_event_in_two_subscriptions_is_sent_twice(self):
        sn, st = FakeSN(), state_mod.State()
        run(cfg(subscription_ids=(SUB, "zweite-sub")), FakeAzure([ev("A")]), sn, st, NOW)
        self.assertEqual(len(sn.sent), 2)


class StateFileTests(unittest.TestCase):
    def test_roundtrip_and_missing_and_corrupt(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "state.json")
            self.assertIsNone(state_mod.load(path))
            st = state_mod.State()
            st.last_run = NOW
            st.mark_sent("k", "Active", "u1")
            state_mod.save(st, path)
            loaded = state_mod.load(path)
            self.assertEqual(loaded.last_run, NOW)
            self.assertFalse(loaded.needs_send("k", "Active", "u1"))
            self.assertTrue(loaded.needs_send("k", "Resolved", "u1"))
            self.assertTrue(loaded.needs_send("k", "Active", "u2"))
            self.assertEqual(os.listdir(d), ["state.json"])  # keine Temp-Datei übrig
            with open(path, "w") as fh:
                fh.write("{kaputt")
            self.assertIsNone(state_mod.load(path))

    def test_fetch_from_gitlab(self):
        def route(req, n):
            if "/jobs/artifacts/" in req["path"]:
                return 200, {"last_run": NOW.isoformat(), "events": {"k": {"status": "Active", "lastUpdateTime": "u"}}}
            return 404, {}

        srv = FakeServer(route)
        self.addCleanup(srv.close)
        env = {
            "CI_API_V4_URL": f"{srv.base}/api/v4", "CI_PROJECT_ID": "42",
            "CI_COMMIT_REF_NAME": "main", "CI_JOB_NAME": "sync-service-health", "CI_JOB_TOKEN": "jt",
        }
        st = state_mod.fetch_from_gitlab("state.json", env=env)
        self.assertEqual(st.last_run, NOW)
        req = srv.received[0]
        self.assertEqual(req["path"], "/api/v4/projects/42/jobs/artifacts/main/raw/state.json")
        self.assertEqual(req["query"], {"job": "sync-service-health"})
        self.assertEqual(req["headers"]["job-token"], "jt")

    def test_fetch_from_gitlab_404_and_outside_gitlab(self):
        srv = FakeServer(lambda req, n: (404, {}))
        self.addCleanup(srv.close)
        env = {"CI_API_V4_URL": f"{srv.base}/api/v4", "CI_PROJECT_ID": "1", "CI_COMMIT_REF_NAME": "main",
               "CI_JOB_NAME": "j", "CI_JOB_TOKEN": "t"}
        self.assertIsNone(state_mod.fetch_from_gitlab("state.json", env=env))
        self.assertIsNone(state_mod.fetch_from_gitlab("state.json", env={}))


if __name__ == "__main__":
    unittest.main()
