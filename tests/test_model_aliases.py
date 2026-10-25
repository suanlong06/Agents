"""Bảng alias → (model_id, vendor) (PR-2, R11): không xung đột, alias lạ là không rõ."""

import tempfile
import unittest
from pathlib import Path

from adapters.base import AliasConflict, ConfigError, ModelAliases, ModelRef
from adapters.router import DEFAULT_ALIASES_PATH, DEFAULT_ROUTING_PATH, RoutingConfig
from orchestrator.config import DEFAULT_PRICES_PATH
from orchestrator.ledger import PriceTable


class RepoAliasTableTest(unittest.TestCase):
    def setUp(self):
        self.aliases = ModelAliases.load(DEFAULT_ALIASES_PATH)

    def test_loads_without_conflict_and_resolves_case_insensitively(self):
        self.assertEqual(self.aliases.resolve("XH/DeepSeek-v4.1-Flash-1017"),
                         ModelRef("deepseek-v4.1-flash-1017", "deepseek"))
        self.assertEqual(self.aliases.resolve(" claude-opus-5-5[1m] "), ModelRef("claude-opus-5-5", "anthropic"))
        self.assertIsNone(self.aliases.resolve("opus"))  # alias "latest" không có trong bảng

    def test_every_routed_model_resolves(self):
        cfg = RoutingConfig.load(DEFAULT_ROUTING_PATH, self.aliases)
        for route in cfg.routes.values():
            for step in route.chain:
                self.assertIsNotNone(self.aliases.resolve(step.model))

    def test_vendors_agree_with_prices_yaml(self):
        prices = PriceTable.load(DEFAULT_PRICES_PATH)
        for model_id, ref in self.aliases.models.items():
            if model_id in prices.models:
                self.assertEqual(prices.vendor(model_id), ref.vendor, model_id)


class AliasConflictTest(unittest.TestCase):
    def test_same_alias_two_targets_is_conflict(self):
        with self.assertRaises(AliasConflict):
            ModelAliases({"a-model": "anthropic", "d-model": "deepseek"},
                         {"fast": "a-model", "FAST": "d-model"})

    def test_alias_shadowing_another_model_id_is_conflict(self):
        with self.assertRaises(AliasConflict):
            ModelAliases({"a-model": "anthropic", "d-model": "deepseek"}, {"D-Model": "a-model"})

    def test_same_model_id_two_vendors_is_conflict(self):
        with self.assertRaises(AliasConflict):
            ModelAliases({"m-1": "anthropic", "M-1": "google"})

    def test_harmless_duplicate_to_same_target_is_allowed(self):
        a = ModelAliases({"m-1": "google"}, {"x/m-1": "m-1", "X/M-1": "m-1"})
        self.assertEqual(a.resolve("x/M-1"), ModelRef("m-1", "google"))

    def test_alias_to_undeclared_model_is_error(self):
        with self.assertRaises(ConfigError):
            ModelAliases({"m-1": "google"}, {"x": "m-2"})

    def test_duplicate_yaml_key_is_error(self):
        text = ("schema_version: 1\nmodels:\n  m-1: {vendor: google}\n  m-2: {vendor: deepseek}\n"
                "aliases:\n  fast: m-1\n  fast: m-2\n")
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "a.yaml"
            p.write_text(text, encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, "trùng"):
                ModelAliases.load(p)

    def test_unknown_alias_is_unknown_model_never_guessed(self):
        a = ModelAliases({"m-1": "google"})
        s = a.served("m-1-preview", "router_log")
        self.assertFalse(s.known)
        self.assertEqual((s.model_id, s.vendor, s.ledger_model), (None, None, "unknown:m-1-preview"))
        self.assertTrue(a.served("M-1", "router_log").known)


if __name__ == "__main__":
    unittest.main()
