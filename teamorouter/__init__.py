"""TeamoRouter model providers for Hermes Agent.

Registers two providers on one TeamoRouter API key:

- ``teamorouter``: Claude models over TeamoRouter's native Anthropic Messages
  endpoint (``https://api.teamorouter.com/v1/messages``), so Hermes applies
  Anthropic prompt caching and thinking exactly as it does for Anthropic.
- ``teamorouter-openai``: GPT, Gemini, DeepSeek, GLM and Grok models over the
  OpenAI-compatible endpoint (``https://api.teamorouter.com/v1``).

Hermes picks one wire protocol per provider, so the split is by provider.
Credential resolution, ``--provider``, ``hermes doctor`` and the ``/model``
picker auto-wire from the provider registry; this module only declares the two
profiles and reads TeamoRouter's public model catalog.

The plugin's own network use: GET /v1/models/pricing on api.teamorouter.com, a
public catalog, sent without the API key. Chat requests are made by Hermes'
own clients with TEAMOROUTER_API_KEY, which Hermes reads; the plugin never
sees, stores or forwards the key. ``teamorouter-openai`` requests carry
``X-Client-Source: hermes-teamorouter-plugin`` (Hermes sends a profile's
``default_headers`` on the OpenAI wire only) so TeamoRouter can count traffic
from this plugin; nothing else is reported.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.request
from typing import Any
from urllib.parse import urlparse

from providers import register_provider
from providers.base import ProviderProfile

logger = logging.getLogger(__name__)

__version__ = "0.1.0"

CLAUDE_PROVIDER_ID = "teamorouter"
OPENAI_PROVIDER_ID = "teamorouter-openai"
API_KEY_ENV = "TEAMOROUTER_API_KEY"

API_HOST = "api.teamorouter.com"
# The Anthropic SDK appends /v1/messages to this.
ANTHROPIC_BASE_URL = "https://api.teamorouter.com"
OPENAI_BASE_URL = "https://api.teamorouter.com/v1"
# Authenticated model list; `hermes doctor` probes it with the key.
MODELS_URL = "https://api.teamorouter.com/v1/models"
PRICING_URL = "https://api.teamorouter.com/v1/models/pricing"
SIGNUP_URL = (
    "https://teamorouter.com/"
    "?utm_source=hermes&utm_medium=plugin&utm_campaign=hermes_catalog"
)

# Source attribution on the OpenAI-compatible wire: TeamoRouter's gateway reads
# this header to label requests that come from this plugin. It carries no user
# data. Hermes does not send profile headers on its Anthropic wire.
CLIENT_SOURCE_HEADER = "X-Client-Source"
CLIENT_SOURCE = "hermes-teamorouter-plugin"
OPENAI_DEFAULT_HEADERS = {CLIENT_SOURCE_HEADER: CLIENT_SOURCE}

# TeamoRouter's catalog has no tool-calling flag, so model families are the
# filter: every family listed here calls tools.
CLAUDE_PREFIX = "claude-"
OPENAI_FAMILIES = ("gpt-", "gemini-", "deepseek-", "glm-", "grok-", "kimi-", "qwen")
EXCLUDED_MARKERS = ("image", "embed", "-exp")
MIN_CONTEXT = 64_000  # Hermes refuses to start a session below this

CATALOG_TTL_SECONDS = 3600.0
FAILED_FETCH_RETRY_SECONDS = 300.0
CONTEXT_LOOKUP_TIMEOUT = 5.0


def is_claude(model_id: str) -> bool:
    return model_id.lower().startswith(CLAUDE_PREFIX)


def listed_in_picker(entry: Any, *, claude: bool) -> bool:
    """True for a token-billed chat model of the requested side (Claude or not).

    Skips per-image models, service-tier variants such as ``gpt-5.5 (fast)`` and
    free-tier variants (both carry ``base_model_id``), dated aliases without a
    context window, and anything with a context window Hermes cannot run.
    """
    if not isinstance(entry, dict):
        return False
    model_id = entry.get("id")
    if not isinstance(model_id, str) or not model_id or any(c.isspace() for c in model_id):
        return False
    if entry.get("billing_mode") != "token" or entry.get("base_model_id"):
        return False
    lowered = model_id.lower()
    if any(marker in lowered for marker in EXCLUDED_MARKERS):
        return False
    family_ok = is_claude(lowered) if claude else lowered.startswith(OPENAI_FAMILIES)
    context = entry.get("context_window")
    return family_ok and type(context) is int and context >= MIN_CONTEXT


def picker_models(entries: list, *, claude: bool) -> list[str]:
    """Picker ids for one side, in catalog order, without duplicates."""
    ids: list[str] = []
    for entry in entries:
        if listed_in_picker(entry, claude=claude) and entry["id"] not in ids:
            ids.append(entry["id"])
    return ids


class _Catalog:
    """Last good /v1/models/pricing answer."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: list[dict] = []
        self._fetched_at = 0.0
        self._failed_at: float | None = None

    def store(self, entries: list) -> None:
        with self._lock:
            self._entries = [e for e in entries if isinstance(e, dict)]
            self._fetched_at = time.monotonic()
            self._failed_at = None

    def mark_failed(self) -> None:
        with self._lock:
            self._failed_at = time.monotonic()

    def fresh(self) -> bool:
        with self._lock:
            return bool(self._entries) and time.monotonic() - self._fetched_at < CATALOG_TTL_SECONDS

    def recently_failed(self) -> bool:
        with self._lock:
            return self._failed_at is not None and time.monotonic() - self._failed_at < FAILED_FETCH_RETRY_SECONDS

    def entries(self) -> list[dict]:
        with self._lock:
            return list(self._entries)

    def context_window(self, model: str) -> int | None:
        with self._lock:
            for entry in self._entries:
                if entry.get("id") == model:
                    context = entry.get("context_window")
                    return context if type(context) is int and context > 0 else None
        return None

    def clear(self) -> None:
        with self._lock:
            self._entries = []
            self._fetched_at = 0.0
            self._failed_at = None


catalog = _Catalog()


def fetch_pricing(*, timeout: float) -> list | None:
    """GET the public pricing catalog. No API key is sent; it is not needed."""
    request = urllib.request.Request(PRICING_URL)
    request.add_header("Accept", "application/json")
    request.add_header("User-Agent", f"hermes-provider-teamorouter/{__version__}")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - a catalog failure must never break Hermes
        logger.debug("teamorouter: GET %s failed: %s", PRICING_URL, exc)
        return None
    entries = payload.get("models") if isinstance(payload, dict) else None
    return entries if isinstance(entries, list) else None


def refresh_catalog(*, timeout: float) -> bool:
    entries = fetch_pricing(timeout=timeout)
    if entries:
        catalog.store(entries)
        return True
    catalog.mark_failed()
    return False


class TeamoRouterProfile(ProviderProfile):
    """Picker and context windows from TeamoRouter's public pricing catalog.

    The Anthropic-wire profile lists Claude models; the OpenAI-wire profile
    lists the other families.
    """

    def _lists_claude(self) -> bool:
        return self.api_mode == "anthropic_messages"

    def fetch_models(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = 8.0,
    ) -> list[str] | None:
        """Tool-capable chat models for this profile, from the catalog.

        A ``base_url`` on another host means the user routes through a proxy
        (``model.base_url``); its catalog is not ours to interpret, so Hermes'
        generic ``{base_url}/models`` fetch handles it. Returning None makes
        Hermes show ``fallback_models``.
        """
        caller_base = (base_url or "").strip()
        if caller_base and (urlparse(caller_base).hostname or "").lower() != API_HOST:
            return super().fetch_models(api_key=api_key, base_url=base_url, timeout=timeout)
        if not catalog.fresh() and not refresh_catalog(timeout=timeout):
            return None
        return picker_models(catalog.entries(), claude=self._lists_claude()) or None

    def get_model_context_length(self, model: str) -> int | None:
        """Context window TeamoRouter publishes for ``model``, or None."""
        if not catalog.fresh() and not catalog.recently_failed():
            refresh_catalog(timeout=CONTEXT_LOOKUP_TIMEOUT)
        return catalog.context_window(model)


teamorouter = TeamoRouterProfile(
    name=CLAUDE_PROVIDER_ID,
    aliases=("teamo-router",),
    display_name="TeamoRouter",
    description="TeamoRouter Claude models (native Anthropic Messages API)",
    signup_url=SIGNUP_URL,
    env_vars=(API_KEY_ENV,),
    base_url=ANTHROPIC_BASE_URL,
    models_url=MODELS_URL,
    auth_type="api_key",
    api_mode="anthropic_messages",
    # Cheap Claude for compression, titles and other side tasks.
    default_aux_model="claude-haiku-4-5",
    # Shown only when the live catalog is unreachable.
    fallback_models=(
        "claude-sonnet-5-5",
        "claude-opus-5-5",
        "claude-haiku-5-5",
        "claude-opus-5",
        "claude-sonnet-5",
        "claude-fable-5-1",
        "claude-haiku-4-5",
    ),
)

teamorouter_openai = TeamoRouterProfile(
    name=OPENAI_PROVIDER_ID,
    display_name="TeamoRouter (OpenAI-compatible)",
    description="TeamoRouter GPT, Gemini, DeepSeek, GLM and Grok models (OpenAI-compatible)",
    signup_url=SIGNUP_URL,
    env_vars=(API_KEY_ENV,),
    base_url=OPENAI_BASE_URL,
    auth_type="api_key",
    api_mode="chat_completions",
    default_headers=dict(OPENAI_DEFAULT_HEADERS),
    # Cheap, 1M-context, vision-capable model for side tasks.
    default_aux_model="gemini-3.5-flash-lite",
    # Shown only when the live catalog is unreachable.
    fallback_models=(
        "gpt-6.1-sol",
        "gpt-5.5",
        "gemini-3.8-flash",
        "gemini-3.5-flash-lite",
        "deepseek-v4-pro",
        "glm-5.3",
        "grok-4.6",
    ),
)

register_provider(teamorouter)
register_provider(teamorouter_openai)
