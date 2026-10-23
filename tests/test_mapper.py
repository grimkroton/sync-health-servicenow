import json
import unittest

from servicehealth_sync.mapper import incident_type, map_event, region_summary, strip_html, truncate

SUB = "def04582-9246-4743-9083-8544b0340c23"
OTHER_SUB = "11111111-2222-3333-4444-555555555555"

DESCRIPTION = (
    "<p><em>You’re receiving this notification because you're associated with Azure tenants. "
    "You can find it under Service Health/</em><a href=\"https://aka.ms/AzureServiceHealthAdvisories\" "
    "target=\"_blank\"><em>Health advisories</em></a><em> using tracking ID: ZHXT-XHG.</em>&nbsp;</p>\n"
    "<p>Starting 21 September 2026, Azure will continue a gradual rollout of enforcement.&nbsp;</p>\n"
    "<p><strong>Action recommended</strong></p>\n"
    "<ul><li>To ensure your users can perform actions, <a href=\"https://learn.microsoft.com/entra/mfa\" "
    "target=\"_blank\">enable MFA for your users</a> by 21 September 2026.&nbsp;</li>"
    "<li>Use Azure CLI version 2.76 or later.&nbsp;</li></ul>\n"
    "<p>If you have questions, see <a href=\"https://aka.ms/azureqa\">https://aka.ms/azureqa</a>.</p>"
)


def region(name, subs):
    return {"impactedRegion": name, "status": "Active", "impactedSubscriptions": subs, "updates": [{"summary": "x"}]}


def make_event(**overrides):
    props = {
        "eventType": "HealthAdvisory", "eventSource": "ServiceHealth", "status": "Active",
        "title": "Action Required: Enable multifactor authentication by 21 September 2026",
        "summary": "<p>kurz</p>", "description": DESCRIPTION, "level": "Warning",
        "impactStartTime": "2026-09-10T22:48:06.11Z", "impactMitigationTime": "2026-09-21T15:24:15Z",
        "lastUpdateTime": "2026-09-18T16:32:36.2250978Z",
        "impact": [{
            "impactedService": "Azure Resource Manager",
            "impactedRegions": [
                region("Australia Central", [SUB]), region("Global", [SUB]),
                region("West Europe", [SUB]), region("Japan East", [OTHER_SUB]),
            ],
        }],
    }
    props.update(overrides)
    return {"id": "Microsoft.ResourceHealth/events/ZHXT-XHG", "name": "ZHXT-XHG", "properties": props}


def map_it(event, **kw):
    return map_event(event, SUB, alert_rule="ServiceHealth-Pull", **kw)


class MapperTests(unittest.TestCase):
    def test_active_health_advisory(self):
        alert = map_it(make_event())
        ess = alert["data"]["essentials"]
        props = alert["data"]["alertContext"]["properties"]
        self.assertEqual(alert["schemaId"], "azureMonitorCommonAlertSchema")
        self.assertEqual(ess["monitorCondition"], "Fired")
        self.assertEqual(ess["severity"], "Sev2")
        self.assertEqual(ess["originAlertId"], "ZHXT-XHG")
        self.assertEqual(ess["alertTargetIDs"], [f"/subscriptions/{SUB}"])
        self.assertTrue(ess["alertId"].endswith("/events/ZHXT-XHG"))
        self.assertEqual(ess["firedDateTime"], "2026-09-10T22:48:06.11Z")
        self.assertNotIn("resolvedDateTime", ess)
        self.assertEqual(props["incidentType"], "ActionRequired")
        self.assertEqual(props["trackingId"], "ZHXT-XHG")
        self.assertEqual(props["service"], "Azure Resource Manager")

    def test_only_regions_of_this_subscription(self):
        props = map_it(make_event())["data"]["alertContext"]["properties"]
        self.assertEqual(props["region"], "Australia Central, Global, West Europe")

    def test_resolved_follows_microsoft_status(self):
        alert = map_it(make_event(status="Resolved"))
        self.assertEqual(alert["data"]["essentials"]["monitorCondition"], "Resolved")
        self.assertEqual(alert["data"]["essentials"]["resolvedDateTime"], "2026-09-21T15:24:15Z")
        self.assertEqual(alert["data"]["alertContext"]["properties"]["stage"], "Resolved")

    def test_active_stays_fired_even_after_mitigation_time(self):
        alert = map_it(make_event(status="Active", impactMitigationTime="2020-01-01T00:00:00Z"))
        self.assertEqual(alert["data"]["essentials"]["monitorCondition"], "Fired")

    def test_html_is_removed_but_structure_and_links_survive(self):
        content = map_it(make_event())["data"]["alertContext"]["properties"]["defaultLanguageContent"]
        self.assertNotIn("<", content)
        self.assertNotIn("&nbsp;", content)
        self.assertNotIn("\xa0", content)
        self.assertIn("- To ensure your users can perform actions", content)
        self.assertIn("- Use Azure CLI version 2.76 or later.", content)
        self.assertIn("(https://learn.microsoft.com/entra/mfa)", content)
        self.assertEqual(content.count("https://aka.ms/azureqa"), 1)

    def test_text_is_truncated(self):
        content = map_it(make_event(), max_text_length=100)["data"]["alertContext"]["properties"]["defaultLanguageContent"]
        self.assertLessEqual(len(content), 100)
        self.assertTrue(content.endswith("…"))

    def test_many_regions_are_summarized(self):
        names = [f"Region {i}" for i in range(60)]
        event = make_event(impact=[{"impactedService": "ARM", "impactedRegions": [region(n, [SUB]) for n in names]}])
        props = map_it(event, max_regions=10)["data"]["alertContext"]["properties"]
        self.assertTrue(props["region"].endswith("(+50 weitere)"))
        self.assertIn("Region 9", props["region"])
        self.assertNotIn("Region 10,", props["region"])

    def test_region_without_subscription_list_is_kept(self):
        event = make_event(impact=[{"impactedService": "ARM", "impactedRegions": [region("Global", [])]}])
        self.assertEqual(map_it(event)["data"]["alertContext"]["properties"]["region"], "Global")

    def test_incident_types(self):
        self.assertEqual(incident_type("ServiceIssue", "x"), "Incident")
        self.assertEqual(incident_type("PlannedMaintenance", "x"), "Maintenance")
        self.assertEqual(incident_type("SecurityAdvisory", "x"), "Security")
        self.assertEqual(incident_type("HealthAdvisory", "Action Required: foo"), "ActionRequired")
        self.assertEqual(incident_type("HealthAdvisory", "Retirement notice"), "Informational")
        self.assertEqual(incident_type("SomethingNew", "x"), "SomethingNew")
        self.assertEqual(incident_type(None, None), "Informational")

    def test_severity_mapping_and_default(self):
        sev = lambda lvl: map_it(make_event(level=lvl))["data"]["essentials"]["severity"]
        self.assertEqual(sev("Critical"), "Sev0")
        self.assertEqual(sev("Error"), "Sev1")
        self.assertEqual(sev("Informational"), "Sev3")
        self.assertEqual(sev("Unbekannt"), "Sev3")

    def test_payload_is_small(self):
        names = [f"Region {i}" for i in range(80)]
        event = make_event(impact=[{"impactedService": "ARM", "impactedRegions": [region(n, [SUB]) for n in names]}])
        self.assertLess(len(json.dumps(map_it(event))), 20000)

    def test_missing_optional_fields_do_not_crash(self):
        alert = map_event({"name": "ABC-123", "properties": {}}, SUB, alert_rule="r")
        self.assertEqual(alert["data"]["essentials"]["monitorCondition"], "Fired")
        self.assertEqual(alert["data"]["alertContext"]["properties"]["region"], "")

    def test_full_field_set_like_a_real_alert(self):
        ctx = map_it(make_event())["data"]["alertContext"]
        for key in ("authorization", "channels", "claims", "caller", "correlationId", "eventSource",
                    "eventTimestamp", "eventDataId", "level", "operationName", "operationId", "properties",
                    "status", "subStatus", "submissionTimestamp", "resourceType"):
            self.assertIn(key, ctx)
        props = ctx["properties"]
        for key in ("title", "service", "region", "communication", "incidentType", "trackingId",
                    "impactStartTime", "impactMitigationTime", "impactedServices", "impactedServicesTableRows",
                    "defaultLanguageTitle", "defaultLanguageContent", "stage", "communicationId",
                    "isHIR", "IsSynthetic", "impactType", "version"):
            self.assertIn(key, props)
        self.assertEqual(ctx["operationName"], "Microsoft.ServiceHealth/actionrequired/action")
        self.assertEqual(props["isHIR"], "false")

    def test_impacted_services_is_json_string_in_azure_format(self):
        props = map_it(make_event())["data"]["alertContext"]["properties"]
        parsed = json.loads(props["impactedServices"])
        self.assertEqual(parsed[0]["ServiceName"], "Azure Resource Manager")
        self.assertEqual([r["RegionName"] for r in parsed[0]["ImpactedRegions"]],
                         ["Australia Central", "Global", "West Europe"])

    def test_communication_keeps_html_unless_too_long(self):
        props = map_it(make_event())["data"]["alertContext"]["properties"]
        self.assertTrue(props["communication"].startswith("<p>"))
        short = map_it(make_event(), max_html_length=200)["data"]["alertContext"]["properties"]["communication"]
        self.assertLessEqual(len(short), 200 + 20)
        self.assertTrue(short.startswith("<p>") and short.endswith("</p>"))
        self.assertNotIn("<em>", short)

    def test_ids_are_stable(self):
        a = map_it(make_event())["data"]["alertContext"]
        b = map_it(make_event())["data"]["alertContext"]
        self.assertEqual(a["correlationId"], b["correlationId"])
        self.assertEqual(a["eventDataId"], b["eventDataId"])
        c = map_it(make_event(lastUpdateTime="2026-10-01T00:00:00Z"))["data"]["alertContext"]
        self.assertEqual(a["correlationId"], c["correlationId"])
        self.assertNotEqual(a["eventDataId"], c["eventDataId"])

    def test_helpers(self):
        self.assertEqual(strip_html(None), "")
        self.assertEqual(truncate("abc", 10), "abc")
        self.assertEqual(region_summary(["a", "b"], 5), "a, b")


if __name__ == "__main__":
    unittest.main()
