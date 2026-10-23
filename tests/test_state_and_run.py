import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from servicehealth_sync import state as state_mod
from servicehealth_sync.azure_client import AzureError
from servicehealth_sync.config import Config
from servicehealth_sync.main import load_state, run
from servicehealth_sync.servicenow_client import ServiceNowError
from tests.helpers import FakeServer
from tests.test_mapper import OTHER_SUB, SUB, make_event, region

NOW = datetime(2026, 10, 15, 8, 0, tzinfo=timezone.utc)


def cfg(**kw):
    base = dict(
        tenant_id="t", client_id="c", client_secret="s",
        sn_instance="x", sn_source="q", sn_connector_sys_id="i", sn_token="tok",
    )
    base.update(kw)
    return Config(**base)


def tracking_id_of(payload):
    return payload["message_key"].rsplit("/", 1)[-1]


def sub_info(sid, state="Enabled"):
    return {"subscriptionId": sid, "displayName": f"Name {sid}", "state": state}


class FakeAzure:
    def __init__(self, events=None, subs=None, error=None, sub_error=None):
        self.events, self.error, self.calls = events or [], error, []
        self.subs = [sub_info(SUB)] if subs is None else subs
        self.sub_error = sub_error

    def list_subscriptions(self):
        if self.sub_error:
            raise self.sub_error
        return self.subs

    def list_events(self, sub, since):
        self.calls.append((sub, since))
        if self.error:
            raise self.error
        return iter(self.events)


class FakeSN:
    def __init__(self, fail_for=()):
        self.sent, self.fail_for = [], set(fail_for)

    def send(self, payload):
        tid = tracking_id_of(payload)
        if tid in self.fail_for:
            raise ServiceNowError("boom")
        self.sent.append(payload)


FUTURE = "2099-01-01T00:00:00Z"


def ev(name="ZHXT-XHG", status="Active", last="2026-09-18T16:32:36Z", end=FUTURE):
    e = make_event(status=status, lastUpdateTime=last, impactMitigationTime=end)
    e["name"] = name
    e["id"] = f"Microsoft.ResourceHealth/events/{name}"
    e["properties"]["impact"] = [{"impactedService": "Azure Resource Manager",
                                  "impactedRegions": [region("Global", [SUB])]}]
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
        self.assertEqual([tracking_id_of(p) for p in sn.sent], ["B"])
        self.assertEqual(sn.sent[0]["resolution_state"], "Closing")
        self.assertEqual(az.calls[0][1], NOW - timedelta(hours=24))

    def test_failed_send_is_retried_next_run_and_last_run_stays(self):
        st = state_mod.State()
        sn = FakeSN(fail_for=["B"])
        self.assertEqual(run(cfg(), FakeAzure([ev("A"), ev("B")]), sn, st, NOW), 1)
        self.assertEqual(len(sn.sent), 1)
        self.assertIsNone(st.last_run)
        sn2 = FakeSN()
        self.assertEqual(run(cfg(), FakeAzure([ev("A"), ev("B")]), sn2, st, NOW), 0)
        self.assertEqual([tracking_id_of(p) for p in sn2.sent], ["B"])

    def test_azure_error_counts_and_continues_with_other_subscriptions(self):
        class Mixed(FakeAzure):
            def list_events(self, sub, since):
                if sub == "bad":
                    raise AzureError("403")
                return iter([ev("A")])

        sn, st = FakeSN(), state_mod.State()
        az = Mixed(subs=[sub_info("bad"), sub_info(SUB)])
        self.assertEqual(run(cfg(), az, sn, st, NOW), 1)
        self.assertEqual(len(sn.sent), 1)
        self.assertIsNone(st.last_run)

    def test_dry_run_sends_nothing_and_keeps_state_empty(self):
        sn, st = FakeSN(), state_mod.State()
        self.assertEqual(run(cfg(dry_run=True), FakeAzure([ev("A")]), sn, st, NOW), 0)
        self.assertEqual(sn.sent, [])
        self.assertEqual(st.events, {})
        self.assertIsNone(st.last_run)

    def test_each_subscription_gets_its_own_event(self):
        class PerSub(FakeAzure):
            def list_events(self, sub, since):
                e = ev("A")
                e["properties"]["impact"][0]["impactedRegions"] = [region("Global", [sub])]
                return iter([e])

        sn = FakeSN()
        run(cfg(), PerSub(subs=[sub_info(SUB), sub_info("zweite-sub")]), sn, state_mod.State(), NOW)
        self.assertEqual(sorted(p["node"] for p in sn.sent), sorted([SUB, "zweite-sub"]))

    def test_one_event_per_impacted_subscription_and_no_duplicates_across_queries(self):
        e = ev("A")
        e["properties"]["impact"][0]["impactedRegions"] = [region("Global", [SUB, OTHER_SUB])]
        sn, st = FakeSN(), state_mod.State()
        az = FakeAzure([e], subs=[sub_info(SUB), sub_info(OTHER_SUB)])  # beide Abfragen liefern dasselbe Event
        run(cfg(), az, sn, st, NOW)
        self.assertEqual(sorted(p["node"] for p in sn.sent), sorted([SUB, OTHER_SUB]))
        self.assertEqual(set(st.events), {f"{SUB}|A", f"{OTHER_SUB}|A"})


class ExpiryTests(unittest.TestCase):
    def test_advisory_with_reached_end_date_is_sent_as_closing(self):
        sn, st = FakeSN(), state_mod.State()
        run(cfg(), FakeAzure([ev("A", end="2026-10-01T00:00:00Z")]), sn, st, NOW)
        self.assertEqual(sn.sent[0]["resolution_state"], "Closing")
        self.assertEqual(st.expiring, {})

    def test_open_advisory_is_tracked_and_closed_when_end_date_passes(self):
        end = NOW + timedelta(days=1)
        sn, st = FakeSN(), state_mod.State()
        run(cfg(), FakeAzure([ev("A", end=end.isoformat())]), sn, st, NOW)
        self.assertEqual(sn.sent[0]["resolution_state"], "New")
        self.assertEqual(list(st.expiring), [f"{SUB}|A"])

        # Azure liefert das Event nicht mehr (keine Änderung) – der Alert wird trotzdem geschlossen
        sn2 = FakeSN()
        later = NOW + timedelta(days=2)
        self.assertEqual(run(cfg(), FakeAzure([]), sn2, st, later), 0)
        self.assertEqual([tracking_id_of(p) for p in sn2.sent], ["A"])
        self.assertEqual(sn2.sent[0]["resolution_state"], "Closing")
        self.assertEqual(st.expiring, {})

        # kein erneutes Schließen im nächsten Lauf
        sn3 = FakeSN()
        run(cfg(), FakeAzure([]), sn3, st, later + timedelta(hours=1))
        self.assertEqual(sn3.sent, [])

    def test_unchanged_open_advisory_is_tracked_without_resending(self):
        end = (NOW + timedelta(days=5)).isoformat()
        st = state_mod.State()
        st.mark_sent(f"{SUB}|A", "Active", "2026-09-18T16:32:36Z")  # früher gesendet, ohne Ablauf-Verfolgung
        sn = FakeSN()
        run(cfg(), FakeAzure([ev("A", end=end)]), sn, st, NOW)
        self.assertEqual(sn.sent, [])
        self.assertIn(f"{SUB}|A", st.expiring)

    def test_old_state_triggers_one_full_scan(self):
        st = state_mod.State()
        run(cfg(), FakeAzure(), FakeSN(), st, NOW)
        self.assertTrue(st.expiry_tracking)
        st.expiry_tracking = False  # Zustand aus einer älteren Version
        az = FakeAzure()
        later = NOW + timedelta(hours=1)
        run(cfg(lookback_days=30, overlap_hours=24), az, FakeSN(), st, later)
        self.assertEqual(az.calls[0][1], later - timedelta(days=30))
        az2 = FakeAzure()
        run(cfg(lookback_days=30, overlap_hours=24), az2, FakeSN(), st, later + timedelta(hours=1))
        self.assertEqual(az2.calls[0][1], later - timedelta(hours=24))

    def test_failed_closing_is_retried(self):
        end = NOW + timedelta(days=1)
        st = state_mod.State()
        run(cfg(), FakeAzure([ev("A", end=end.isoformat())]), FakeSN(), st, NOW)
        later = NOW + timedelta(days=2)
        self.assertEqual(run(cfg(), FakeAzure([]), FakeSN(fail_for=["A"]), st, later), 1)
        self.assertIn(f"{SUB}|A", st.expiring)
        self.assertEqual(run(cfg(), FakeAzure([]), FakeSN(), st, later), 0)
        self.assertEqual(st.expiring, {})

    def test_state_roundtrip_keeps_expiring_entries(self):
        st = state_mod.State()
        run(cfg(), FakeAzure([ev("A", end=(NOW + timedelta(days=1)).isoformat())]), FakeSN(), st, NOW)
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "state.json")
            state_mod.save(st, path)
            loaded = state_mod.load(path)
        self.assertTrue(loaded.expiry_tracking)
        self.assertEqual(list(loaded.expiring), [f"{SUB}|A"])
        self.assertEqual(len(loaded.due_expiries(NOW + timedelta(days=2))), 1)


class ForceResendTests(unittest.TestCase):
    def test_force_resend_ignores_saved_state(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "state.json")
            st = state_mod.State()
            run(cfg(), FakeAzure([ev("A")]), FakeSN(), st, NOW)
            state_mod.save(st, path)
            c = cfg(state_file=path)

            with mock.patch.dict(os.environ, {"FORCE_RESEND": "false"}):
                self.assertFalse(load_state(c).needs_send(f"{SUB}|A", "Active", "2026-09-18T16:32:36Z"))
            with mock.patch.dict(os.environ, {"FORCE_RESEND": "true"}):
                forced = load_state(c)
            self.assertEqual(forced.events, {})
            sn = FakeSN()
            run(c, FakeAzure([ev("A")]), sn, forced, NOW + timedelta(hours=1))
            self.assertEqual([tracking_id_of(p) for p in sn.sent], ["A"])


class DiscoveryTests(unittest.TestCase):
    def test_all_visible_active_subscriptions_are_used(self):
        az = FakeAzure([ev("A")], subs=[
            sub_info("s1"), sub_info("s2"), sub_info("s3", "Disabled"), sub_info("s4", "Deleted"), sub_info("s5", "Warned"),
        ])
        run(cfg(), az, FakeSN(), state_mod.State(), NOW)
        self.assertEqual([c[0] for c in az.calls], ["s1", "s2", "s5"])

    def test_allow_list_restricts_and_exclude_list_removes(self):
        az = FakeAzure(subs=[sub_info("S1"), sub_info("s2"), sub_info("s3")])
        run(cfg(subscription_ids=("s1", "s3", "gibt-es-nicht"), exclude_subscription_ids=("S3",)),
            az, FakeSN(), state_mod.State(), NOW)
        self.assertEqual([c[0] for c in az.calls], ["S1"])

    def test_discovery_error_fails_the_run(self):
        az = FakeAzure(sub_error=AzureError("403"))
        st = state_mod.State()
        self.assertEqual(run(cfg(), az, FakeSN(), st, NOW), 1)
        self.assertEqual(az.calls, [])
        self.assertIsNone(st.last_run)

    def test_no_subscriptions_is_an_error(self):
        self.assertEqual(run(cfg(), FakeAzure(subs=[]), FakeSN(), state_mod.State(), NOW), 1)

    def test_new_subscription_gets_full_lookback_known_one_gets_overlap(self):
        st = state_mod.State()
        run(cfg(), FakeAzure(subs=[sub_info("alt")]), FakeSN(), st, NOW)
        self.assertTrue(st.is_known_subscription("ALT"))

        later = NOW + timedelta(days=5)
        az = FakeAzure(subs=[sub_info("alt"), sub_info("neu")])
        run(cfg(lookback_days=30, overlap_hours=24), az, FakeSN(), st, later)
        since = dict(az.calls)
        self.assertEqual(since["alt"], NOW - timedelta(hours=24))
        self.assertEqual(since["neu"], later - timedelta(days=30))
        self.assertTrue(st.is_known_subscription("neu"))

    def test_subscription_with_send_error_stays_new(self):
        st = state_mod.State()
        run(cfg(), FakeAzure([ev("A")], subs=[sub_info("s1")]), FakeSN(fail_for=["A"]), st, NOW)
        self.assertFalse(st.is_known_subscription("s1"))


class StateFileTests(unittest.TestCase):
    def test_roundtrip_and_missing_and_corrupt(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "state.json")
            self.assertIsNone(state_mod.load(path))
            st = state_mod.State()
            st.last_run = NOW
            st.mark_sent("k", "Active", "u1")
            st.mark_subscription("ABC")
            state_mod.save(st, path)
            loaded = state_mod.load(path)
            self.assertTrue(loaded.is_known_subscription("abc"))
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
