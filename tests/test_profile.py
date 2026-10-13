"""Offline tests for the TeamoRouter provider profiles (stub Hermes, no network)."""

from __future__ import annotations

import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import (  # noqa: E402
    CATALOG,
    EXPECTED_CLAUDE,
    EXPECTED_OPENAI,
    INHERITED_MARKER,
    FakeUrlopen,
    load_plugin,
)

FAKE_KEY = "sk-teamo-test-not-a-real-key"
SOURCE_HEADERS = {"X-Client-Source": "hermes-teamorouter-plugin"}


def _profiles():
    plugin, registered = load_plugin()
    by_name = {p.name: p for p in registered}
    return plugin, registered, by_name["teamorouter"], by_name["teamorouter-openai"]


class ProfileDeclarationTests(unittest.TestCase):
    def setUp(self):
        self.plugin, self.registered, self.claude, self.openai = _profiles()

    def test_registers_the_two_providers(self):
        self.assertEqual([p.name for p in self.registered], ["teamorouter", "teamorouter-openai"])
        for profile in self.registered:
            self.assertEqual(type(profile).__name__, "TeamoRouterProfile")

    def test_claude_goes_over_the_native_anthropic_messages_api(self):
        self.assertEqual(self.claude.api_mode, "anthropic_messages")
        # The Anthropic SDK appends /v1/messages to the base URL.
        self.assertEqual(self.claude.base_url, "https://api.teamorouter.com")
        self.assertEqual(self.claude.models_url, "https://api.teamorouter.com/v1/models")
        self.assertEqual(self.claude.aliases, ("teamo-router",))

    def test_other_models_go_over_the_openai_compatible_api(self):
        self.assertEqual(self.openai.api_mode, "chat_completions")
        self.assertEqual(self.openai.base_url, "https://api.teamorouter.com/v1")

    def test_both_use_one_api_key(self):
        for profile in self.registered:
            self.assertEqual(profile.env_vars, ("TEAMOROUTER_API_KEY",))
            self.assertEqual(profile.auth_type, "api_key")

    def test_source_header_only_where_hermes_sends_it(self):
        # Hermes sends profile.default_headers on every OpenAI-wire request
        # (main and auxiliary clients); its Anthropic client never reads them,
        # so the Claude profile declares none rather than claim one.
        self.assertEqual(self.openai.default_headers, SOURCE_HEADERS)
        self.assertEqual(self.claude.default_headers, {})

    def test_signup_url_is_the_tracked_homepage(self):
        for profile in self.registered:
            url = urlparse(profile.signup_url)
            self.assertEqual((url.scheme, url.netloc, url.path), ("https", "teamorouter.com", "/"))
            self.assertEqual(
                parse_qs(url.query),
                {"utm_source": ["hermes"], "utm_medium": ["plugin"], "utm_campaign": ["hermes_catalog"]},
            )

    def test_fallback_and_aux_models_belong_to_each_side(self):
        for profile, expected in ((self.claude, EXPECTED_CLAUDE), (self.openai, EXPECTED_OPENAI)):
            fallback = list(profile.fallback_models)
            self.assertTrue(fallback)
            self.assertEqual(len(fallback), len(set(fallback)))
            for model in [*fallback, profile.default_aux_model]:
                self.assertIn(model, expected, (profile.name, model))


class PickerFilterTests(unittest.TestCase):
    def setUp(self):
        self.plugin, _, _, _ = _profiles()

    def test_fixture_splits_into_the_expected_pickers(self):
        self.assertEqual(self.plugin.picker_models(CATALOG["models"], claude=True), EXPECTED_CLAUDE)
        self.assertEqual(self.plugin.picker_models(CATALOG["models"], claude=False), EXPECTED_OPENAI)

    def test_excluded_entries(self):
        def listed(entry, claude=False):
            return self.plugin.listed_in_picker(entry, claude=claude)

        self.assertFalse(listed({"id": "gpt-5.5 (fast)", "billing_mode": "token", "context_window": 10**6}))
        self.assertFalse(listed({"id": "deepseek-v4-flash-free", "base_model_id": "deepseek-v4-flash",
                                 "billing_mode": "token", "context_window": 10**6}))
        self.assertFalse(listed({"id": "gpt-image-2", "billing_mode": "per_image", "context_window": None}))
        self.assertFalse(listed({"id": "claude-haiku-4-5-20251001", "billing_mode": "token"}, claude=True))
        self.assertFalse(listed({"id": "gemini-x-image", "billing_mode": "token", "context_window": 10**6}))
        self.assertFalse(listed({"id": "deepseek-v4-flash-vision-exp", "billing_mode": "token",
                                 "context_window": 10**6}))
        self.assertFalse(listed({"id": "claude-small", "billing_mode": "token", "context_window": 32000}, True))
        self.assertFalse(listed({"id": "claude-bool", "billing_mode": "token", "context_window": True}, True))
        self.assertFalse(listed({"id": "llama-9", "billing_mode": "token", "context_window": 10**6}))
        self.assertFalse(listed("claude-sonnet-5", claude=True))
        self.assertFalse(listed({"billing_mode": "token", "context_window": 10**6}))

    def test_each_side_rejects_the_other(self):
        claude = {"id": "claude-sonnet-5", "billing_mode": "token", "context_window": 200000}
        gpt = {"id": "gpt-5.5", "billing_mode": "token", "context_window": 200000}
        self.assertTrue(self.plugin.listed_in_picker(claude, claude=True))
        self.assertFalse(self.plugin.listed_in_picker(claude, claude=False))
        self.assertTrue(self.plugin.listed_in_picker(gpt, claude=False))
        self.assertFalse(self.plugin.listed_in_picker(gpt, claude=True))


class FetchModelsTests(unittest.TestCase):
    def setUp(self):
        self.plugin, _, self.claude, self.openai = _profiles()
        self.plugin.catalog.clear()

    def _fetch(self, profile, fake, **kwargs):
        with mock.patch("urllib.request.urlopen", fake):
            return profile.fetch_models(**kwargs)

    def test_reads_public_catalog_without_the_key(self):
        fake = FakeUrlopen()
        models = self._fetch(self.claude, fake, api_key=FAKE_KEY, base_url="https://api.teamorouter.com")
        self.assertEqual(models, EXPECTED_CLAUDE)
        self.assertEqual(len(fake.requests), 1)
        sent = fake.requests[0]
        self.assertEqual(sent["url"], "https://api.teamorouter.com/v1/models/pricing")
        self.assertEqual(sent["method"], "GET")
        self.assertEqual(sent["headers"].get("accept"), "application/json")
        self.assertEqual(sent["headers"].get("user-agent"), "hermes-provider-teamorouter/0.1.0")
        self.assertNotIn("authorization", sent["headers"])
        self.assertNotIn("x-api-key", sent["headers"])
        self.assertFalse(any(FAKE_KEY in v for v in sent["headers"].values()))

    def test_both_providers_share_one_cached_catalog(self):
        fake = FakeUrlopen()
        self.assertEqual(self._fetch(self.claude, fake), EXPECTED_CLAUDE)
        self.assertEqual(self._fetch(self.openai, fake, base_url="https://api.teamorouter.com/v1/"),
                         EXPECTED_OPENAI)
        self.assertEqual(self._fetch(self.claude, fake, base_url="https://api.teamorouter.com/v1"),
                         EXPECTED_CLAUDE)
        self.assertEqual(len(fake.requests), 1)

    def test_network_failure_returns_none_for_fallback(self):
        fake = FakeUrlopen(error=urllib.error.URLError("down"))
        self.assertIsNone(self._fetch(self.claude, fake))
        self.assertIsNone(self._fetch(self.openai, fake))

    def test_malformed_payloads_return_none(self):
        for payload in (b"not json", {"data": []}, {"models": "nope"}, [], {"models": []}):
            self.plugin.catalog.clear()
            self.assertIsNone(self._fetch(self.openai, FakeUrlopen(payload=payload)), payload)

    def test_catalog_without_models_for_a_side_returns_none(self):
        payload = {"models": [{"id": "gpt-5.5", "billing_mode": "token", "context_window": 400000}]}
        self.assertIsNone(self._fetch(self.claude, FakeUrlopen(payload=payload)))

    def test_proxy_base_url_defers_to_hermes_generic_fetch(self):
        fake = FakeUrlopen()
        result = self._fetch(self.openai, fake, api_key=FAKE_KEY, base_url="https://proxy.example.test/v1")
        self.assertEqual(result, INHERITED_MARKER)
        self.assertEqual(fake.requests, [])
        self.assertEqual(
            self.openai.inherited_calls,
            [{"api_key": FAKE_KEY, "base_url": "https://proxy.example.test/v1", "timeout": 8.0}],
        )


class ContextLengthTests(unittest.TestCase):
    def setUp(self):
        self.plugin, _, self.claude, self.openai = _profiles()
        self.plugin.catalog.clear()

    def _context(self, profile, fake, model):
        with mock.patch("urllib.request.urlopen", fake):
            return profile.get_model_context_length(model)

    def test_context_windows_come_from_the_catalog(self):
        fake = FakeUrlopen()
        self.assertEqual(self._context(self.claude, fake, "claude-opus-5-5"), 1_000_000)
        self.assertEqual(self._context(self.claude, fake, "claude-haiku-4-5"), 200_000)
        self.assertEqual(self._context(self.openai, fake, "grok-4.6"), 500_000)
        self.assertEqual(len(fake.requests), 1)
        self.assertEqual(fake.requests[0]["timeout"], self.plugin.CONTEXT_LOOKUP_TIMEOUT)

    def test_unknown_or_null_context_is_none(self):
        fake = FakeUrlopen()
        self.assertIsNone(self._context(self.openai, fake, "no-such-model"))
        self.assertIsNone(self._context(self.openai, fake, "gpt-image-2"))

    def test_failed_lookup_is_not_retried_immediately(self):
        fake = FakeUrlopen(error=urllib.error.URLError("down"))
        self.assertIsNone(self._context(self.claude, fake, "claude-opus-5-5"))
        self.assertIsNone(self._context(self.claude, fake, "claude-opus-5-5"))
        self.assertEqual(len(fake.requests), 1)

    def test_picker_fetch_warms_the_context_lookup(self):
        fake = FakeUrlopen()
        with mock.patch("urllib.request.urlopen", fake):
            self.claude.fetch_models()
            self.assertEqual(self.claude.get_model_context_length("claude-sonnet-5-5"), 1_000_000)
        self.assertEqual(len(fake.requests), 1)


if __name__ == "__main__":
    unittest.main()
