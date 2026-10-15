import os
import unittest
from unittest import mock

from servicehealth_sync.config import Config, ConfigError

BASE = {
    "AZURE_TENANT_ID": "t", "AZURE_CLIENT_ID": "c", "AZURE_CLIENT_SECRET": "s",
    "AZURE_SUBSCRIPTION_IDS": "sub1, sub2;sub3",
    "SERVICENOW_INSTANCE": "https://beispiel.service-now.com/",
    "SERVICENOW_SOURCE": "quelle", "SERVICENOW_CONNECTOR_SYS_ID": "abc",
    "SERVICENOW_TOKEN": "tok",
}


class ConfigTests(unittest.TestCase):
    def load(self, **extra):
        with mock.patch.dict(os.environ, {**BASE, **extra}, clear=True):
            return Config.from_env()

    def test_loads_and_normalizes(self):
        cfg = self.load()
        self.assertEqual(cfg.subscription_ids, ("sub1", "sub2", "sub3"))
        self.assertEqual(cfg.sn_instance, "beispiel.service-now.com")
        self.assertEqual(cfg.lookback_days, 30)
        self.assertFalse(cfg.dry_run)

    def test_no_username_required(self):
        self.assertEqual(self.load().sn_token, "tok")

    def test_missing_variable(self):
        env = {k: v for k, v in BASE.items() if k != "SERVICENOW_TOKEN"}
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(ConfigError) as ctx:
                Config.from_env()
        self.assertIn("SERVICENOW_TOKEN", str(ctx.exception))

    def test_invalid_integer(self):
        with self.assertRaises(ConfigError):
            self.load(LOOKBACK_DAYS="viele")

    def test_dry_run_flag(self):
        self.assertTrue(self.load(DRY_RUN="true").dry_run)


if __name__ == "__main__":
    unittest.main()
