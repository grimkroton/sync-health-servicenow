import json
import unittest
from datetime import datetime, timezone

from servicehealth_sync.event_mapper import closing_payload, impacted_nodes, map_event, snow_time
from tests.test_mapper import OTHER_SUB, SUB, make_event, region

REQUIRED = {
    "source", "event_class", "node", "resource", "metric_name", "type", "severity",
    "description", "message_key", "time_of_event", "resolution_state", "additional_info",
}

FUTURE = "2099-01-01T00:00:00Z"
NOW = datetime(2026, 10, 15, 8, 0, tzinfo=timezone.utc)


def events(**kw):
    kw.setdefault("impactMitigationTime", FUTURE)
    return map_event(make_event(**kw), SUB)


def record(**kw):
    """Das Event für die Subscription SUB."""
    return next(r for r in events(**kw) if r["node"] == SUB)


def only_sub_event(**kw):
    """Testevent, das nur SUB betrifft (ein Service, eine Region)."""
    event = make_event(**kw)
    event["properties"]["impact"] = [{"impactedService": "Azure Resource Manager",
                                      "impactedRegions": [region("Global", [SUB])]}]
    return event


class EventMapperTests(unittest.TestCase):
    def test_field_mapping(self):
        rec = record()
        self.assertEqual(set(rec), REQUIRED)
        self.assertEqual(rec["source"], "ServiceHealth")  # eventSource
        self.assertEqual(rec["event_class"], "LHM Azure Service Health")
        self.assertEqual(rec["node"], SUB)
        self.assertEqual(rec["resource"], "Azure Resource Manager")  # impactedService
        self.assertEqual(rec["metric_name"], "")
        self.assertEqual(rec["type"], "HealthAdvisory")  # eventType
        self.assertEqual(rec["description"], "Action Required: Enable multifactor authentication by 21 September 2026")  # title
        self.assertEqual(rec["message_key"], "Microsoft.ResourceHealth/events/ZHXT-XHG")  # id
        self.assertEqual(rec["time_of_event"], "2026-09-18 16:32:36")
        self.assertEqual(rec["resolution_state"], "New")

    def test_additional_info_is_the_plain_text_description(self):
        info = record()["additional_info"]
        self.assertIn("Starting 21 September 2026", info)
        self.assertNotIn("<p>", info)

    def test_additional_info_is_limited(self):
        rec = map_event(only_sub_event(), SUB, max_text_length=50)[0]
        self.assertLessEqual(len(rec["additional_info"]), 50)

    def test_one_event_per_impacted_subscription(self):
        recs = events()
        self.assertEqual(sorted(r["node"] for r in recs), sorted([SUB, OTHER_SUB]))
        self.assertEqual(len({r["message_key"] for r in recs}), 1)

    def test_subscription_listed_in_several_regions_gives_one_event(self):
        recs = map_event(only_sub_event(), SUB)
        self.assertEqual([r["node"] for r in recs], [SUB])

    def test_resource_lists_only_services_of_that_node(self):
        event = make_event()
        event["properties"]["impact"] = [
            {"impactedService": "Azure OpenAI", "impactedRegions": [region("Sweden Central", [SUB])]},
            {"impactedService": "Azure Storage", "impactedRegions": [region("Global", [OTHER_SUB]), region("West", [SUB])]},
        ]
        by_node = {r["node"]: r["resource"] for r in map_event(event, SUB)}
        self.assertEqual(by_node[SUB], "Azure OpenAI, Azure Storage")
        self.assertEqual(by_node[OTHER_SUB], "Azure Storage")

    def test_queried_subscription_is_the_node_when_api_lists_none(self):
        event = make_event()
        event["properties"]["impact"] = [{"impactedService": "X", "impactedRegions": [region("Global", [])]}]
        recs = map_event(event, SUB)
        self.assertEqual([r["node"] for r in recs], [SUB])
        self.assertEqual(recs[0]["resource"], "X")
        self.assertEqual(impacted_nodes({}, SUB), {SUB: []})

    def test_event_source_comes_from_the_api(self):
        self.assertEqual(record(eventSource="Foo")["source"], "Foo")

    def test_severity_follows_the_azure_level(self):
        self.assertEqual(record(level="Critical")["severity"], "1")
        self.assertEqual(record(level="Error")["severity"], "2")
        self.assertEqual(record(level="Warning")["severity"], "3")
        self.assertEqual(record(level="Informational")["severity"], "5")
        self.assertEqual(record(level="Unbekannt")["severity"], "5")

    def test_severity_does_not_depend_on_event_type(self):
        self.assertEqual(record(eventType="ServiceIssue", level="Warning")["severity"], "3")
        self.assertEqual(record(eventType="HealthAdvisory", level="Warning")["severity"], "3")

    def test_resolved_event_closes_with_same_message_key(self):
        fired = record(status="Active")
        closed = record(status="Resolved")
        self.assertEqual(closed["resolution_state"], "Closing")
        self.assertEqual(closed["severity"], "0")
        self.assertEqual(fired["message_key"], closed["message_key"])

    def test_payload_is_valid_json_with_only_strings(self):
        for rec in events():
            json.dumps(rec)
            for key, value in rec.items():
                self.assertIsInstance(value, str, key)

    def test_advisory_with_reached_end_date_is_closed(self):
        # make_event: HealthAdvisory, Status Active, Enddatum 21.09.2026 – vor NOW
        out = map_event(make_event(), SUB, now=NOW)[0]
        self.assertEqual(out["resolution_state"], "Closing")
        self.assertEqual(out["severity"], "0")
        self.assertEqual(out["time_of_event"], "2026-09-21 15:24:15")

    def test_end_date_only_closes_advisories(self):
        out = map_event(make_event(eventType="ServiceIssue"), SUB, now=NOW)[0]
        self.assertEqual(out["resolution_state"], "New")
        self.assertEqual(out["type"], "ServiceIssue")

    def test_placeholder_end_date_is_ignored(self):
        out = map_event(make_event(impactMitigationTime="0001-01-01T00:00:00"), SUB, now=NOW)[0]
        self.assertEqual(out["resolution_state"], "New")

    def test_title_html_entities_are_cleaned(self):
        out = record(title="Retirement notice:&nbsp; Python 2.7 &amp; 3.8")
        self.assertEqual(out["description"], "Retirement notice: Python 2.7 & 3.8")

    def test_closing_payload_closes_a_stored_alert(self):
        fired = map_event(only_sub_event(impactMitigationTime=FUTURE), SUB, now=NOW)[0]
        closed = closing_payload(fired, FUTURE)
        self.assertEqual(closed["resolution_state"], "Closing")
        self.assertEqual(closed["severity"], "0")
        self.assertEqual(closed["message_key"], fired["message_key"])
        self.assertEqual(closed["time_of_event"], "2099-01-01 00:00:00")
        self.assertEqual(fired["resolution_state"], "New")  # Original unverändert

    def test_snow_time_format(self):
        self.assertEqual(snow_time("2026-09-10T22:48:06.11Z"), "2026-09-10 22:48:06")
        self.assertEqual(snow_time("2026-10-06T14:15:00+02:00"), "2026-10-06 12:15:00")
        self.assertEqual(snow_time("2026-10-06T12:15:00"), "2026-10-06 12:15:00")


if __name__ == "__main__":
    unittest.main()
