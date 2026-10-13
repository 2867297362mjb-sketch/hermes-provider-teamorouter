"""Load the plugin against stub ``providers`` modules, so unit tests run without Hermes."""

from __future__ import annotations

import importlib.util
import io
import json
import sys
import types
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "teamorouter"
TESTS_DIR = Path(__file__).resolve().parent

# Snapshot of the public https://api.teamorouter.com/v1/models/pricing response
# (2026-10-14), trimmed to the fields the plugin reads, plus a few synthetic
# edge cases at the end (a duplicate id, tiny contexts, an embedding model, a
# non-dict entry).
CATALOG = json.loads((TESTS_DIR / "pricing_fixture.json").read_text(encoding="utf-8"))

# What each picker must show for that fixture, in catalog order.
EXPECTED_CLAUDE = [
    "claude-sonnet-5-5", "claude-haiku-5-5", "claude-opus-5-5", "claude-opus-5",
    "claude-fable-5-1", "claude-fable-5", "claude-sonnet-5", "claude-opus-4-8",
    "claude-opus-4-7", "claude-sonnet-4-6", "claude-opus-4-6", "claude-haiku-4-5",
]
EXPECTED_OPENAI = [
    "gpt-6.1-sol", "gpt-6-sol", "gpt-6-luna", "gpt-6-astra", "gpt-5.6-sol",
    "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.5", "gpt-5.4-mini", "gpt-5.4",
    "gemini-4-argon", "gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash",
    "gemini-3.5-flash", "gemini-3.5-flash-lite", "gemini-3.1-pro-preview",
    "deepseek-flash", "deepseek-v4-pro", "deepseek-v4-flash",
    "glm-5.3-flash", "glm-5.3", "glm-5.2",
    "grok-4.6",
]

# Returned by the stub base-class fetch_models, so a test can tell that the
# plugin delegated to Hermes' generic /models path.
INHERITED_MARKER = ["__inherited_fetch_models__"]


@dataclass
class StubProviderProfile:
    """The ``providers.base.ProviderProfile`` fields this plugin sets."""

    name: str
    api_mode: str = "chat_completions"
    aliases: tuple = ()
    display_name: str = ""
    description: str = ""
    signup_url: str = ""
    env_vars: tuple = ()
    base_url: str = ""
    models_url: str = ""
    auth_type: str = "api_key"
    fallback_models: tuple = ()
    hostname: str = ""
    default_headers: dict = field(default_factory=dict)
    default_aux_model: str = ""

    def fetch_models(self, *, api_key=None, base_url=None, timeout=8.0):
        self.inherited_calls = getattr(self, "inherited_calls", [])
        self.inherited_calls.append({"api_key": api_key, "base_url": base_url, "timeout": timeout})
        return list(INHERITED_MARKER)

    def get_model_context_length(self, model):
        return None


def load_plugin():
    """Import the plugin fresh; return ``(module, [registered profiles])``."""
    registered: list = []
    providers = types.ModuleType("providers")
    providers.register_provider = registered.append
    base = types.ModuleType("providers.base")
    base.ProviderProfile = StubProviderProfile
    providers.base = base
    saved = {name: sys.modules.get(name) for name in ("providers", "providers.base")}
    sys.modules.update({"providers": providers, "providers.base": base})
    try:
        spec = importlib.util.spec_from_file_location(
            "teamorouter_provider_under_test",
            PLUGIN_DIR / "__init__.py",
            submodule_search_locations=[str(PLUGIN_DIR)],
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        for name, previous in saved.items():
            if previous is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous
    return module, registered


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class FakeUrlopen:
    """Stands in for ``urllib.request.urlopen``; records every request."""

    def __init__(self, payload: Any = None, error: Exception | None = None):
        self.payload = CATALOG if payload is None else payload
        self.error = error
        self.requests: list = []

    def __call__(self, request, timeout=None):
        self.requests.append({
            "url": request.full_url,
            "method": request.get_method(),
            "headers": {k.lower(): v for k, v in request.header_items()},
            "timeout": timeout,
        })
        if self.error is not None:
            raise self.error
        body = self.payload if isinstance(self.payload, bytes) else json.dumps(self.payload).encode()
        return FakeResponse(body)
