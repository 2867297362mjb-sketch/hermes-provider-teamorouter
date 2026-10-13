"""End-to-end against a real Hermes checkout. Skipped when ``hermes_cli`` is not importable.

Run with the checkout's interpreter, e.g.
``PYTHONPATH=/path/to/hermes-agent /path/to/hermes-agent/.venv/bin/python -m unittest discover -s tests``.

No real network: the catalog request is answered from the fixture, OpenAI-wire
chat goes to Hermes' fake LLM server, Anthropic-wire chat goes to a minimal
local Messages server below, and every other outbound HTTP(S) request is pointed
at a dead proxy.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import EXPECTED_CLAUDE, EXPECTED_OPENAI, PLUGIN_DIR, TESTS_DIR  # noqa: E402

try:
    import hermes_cli  # noqa: F401

    HERMES_ROOT = Path(hermes_cli.__file__).resolve().parents[1]
except ImportError:  # pragma: no cover - unit-only environments
    HERMES_ROOT = None

API_KEY = "sk-teamo-e2e-not-a-real-key"
SOURCE_HEADER = ("x-client-source", "hermes-teamorouter-plugin")


class FakeMessagesServer:
    """Minimal Anthropic Messages endpoint: records each POST, answers "pong"."""

    def __init__(self):
        self.requests: list = []
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):  # noqa: N802
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                body = json.loads(raw or b"{}")
                server.requests.append({"path": self.path, "body": body,
                                        "headers": {k.lower(): v for k, v in self.headers.items()}})
                if self.path.split("?", 1)[0].endswith("/count_tokens"):
                    return self._json({"input_tokens": 12})
                if not self.path.split("?", 1)[0].endswith("/v1/messages"):
                    return self._json({"type": "error", "error": {"type": "not_found_error",
                                                                  "message": self.path}}, 404)
                model = body.get("model", "")
                usage = {"input_tokens": 12, "output_tokens": 2}
                if not body.get("stream"):
                    return self._json({"id": "msg_e2e", "type": "message", "role": "assistant", "model": model,
                                       "content": [{"type": "text", "text": "pong"}], "stop_reason": "end_turn",
                                       "stop_sequence": None, "usage": usage})
                events = [
                    {"type": "message_start", "message": {
                        "id": "msg_e2e", "type": "message", "role": "assistant", "model": model, "content": [],
                        "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 12, "output_tokens": 1}}},
                    {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
                    {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "pong"}},
                    {"type": "content_block_stop", "index": 0},
                    {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                     "usage": {"output_tokens": 2}},
                    {"type": "message_stop"},
                ]
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                for event in events:
                    self.wfile.write(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode())
                    self.wfile.flush()

            def _json(self, payload, status=200):
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        # No path: the Anthropic SDK appends /v1/messages, as for https://api.teamorouter.com.
        self.base_url = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@unittest.skipIf(HERMES_ROOT is None, "Hermes is not importable")
class HermesIntegrationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="teamorouter-e2e-")
        self.home = Path(self._tmp.name) / "hermes-home"
        shutil.copytree(PLUGIN_DIR, self.home / "plugins" / "teamorouter",
                        ignore=shutil.ignore_patterns("__pycache__"))
        (self.home / ".env").write_text(f"TEAMOROUTER_API_KEY={API_KEY}\n", encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def _env(self) -> dict:
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("OPENAI", "ANTHROPIC", "TEAMOROUTER", "OPENROUTER", "HERMES_"))
               and k.lower() not in {"http_proxy", "https_proxy", "all_proxy", "no_proxy"}}
        dead_proxy = "http://127.0.0.1:9"  # discard port: any stray outbound request fails fast
        env.update(HERMES_HOME=str(self.home), PYTHONPATH=str(HERMES_ROOT), NO_COLOR="1",
                   HTTP_PROXY=dead_proxy, HTTPS_PROXY=dead_proxy, ALL_PROXY=dead_proxy,
                   NO_PROXY="127.0.0.1,localhost")
        return env

    def _python(self, code: str):
        proc = subprocess.run([sys.executable, "-c", code], env=self._env(), cwd=str(self.home.parent),
                              capture_output=True, text=True, timeout=180, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr[-3000:])
        return json.loads(proc.stdout.strip().splitlines()[-1])

    def _turn(self, provider: str, model: str, base_url: str):
        (self.home / "config.yaml").write_text(
            f"model:\n  provider: {provider}\n"
            f"  base_url: {base_url}\n  default: {model}\n  context_length: 1000000\n"
            "agent:\n  api_max_retries: 1\n", encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, "-m", "hermes_cli.main", "chat", "-q", "Reply with: pong",
             "--oneshot", "-Q", "-t", "todo"],
            env=self._env(), cwd=str(self.home.parent), capture_output=True, text=True,
            timeout=300, check=False)
        self.assertEqual(proc.returncode, 0, (proc.stdout[-1500:], proc.stderr[-3000:]))
        self.assertIn("pong", proc.stdout)

    def test_hermes_discovers_both_providers(self):
        found = self._python("""
import json
from providers import get_provider_profile
from hermes_cli.auth import PROVIDER_REGISTRY
from hermes_cli.config import OPTIONAL_ENV_VARS
from hermes_cli.models import list_available_providers
out = {"setup_lists_key": "TEAMOROUTER_API_KEY" in OPTIONAL_ENV_VARS,
       "alias_resolves": get_provider_profile("teamo-router") is get_provider_profile("teamorouter")}
listed = {p["id"]: p["authenticated"] for p in list_available_providers()}
for name in ("teamorouter", "teamorouter-openai"):
    profile = get_provider_profile(name)
    row = PROVIDER_REGISTRY.get(name)
    out[name] = {"cls": type(profile).__name__, "base_url": profile.base_url, "api_mode": profile.api_mode,
                 "headers": profile.default_headers, "env": list(profile.env_vars),
                 "row_url": row.inference_base_url, "row_env": list(row.api_key_env_vars),
                 "authenticated": listed.get(name)}
print(json.dumps(out))
""")
        self.assertEqual(found, {
            "setup_lists_key": True, "alias_resolves": True,
            "teamorouter": {
                "cls": "TeamoRouterProfile", "base_url": "https://api.teamorouter.com",
                "api_mode": "anthropic_messages", "headers": {}, "env": ["TEAMOROUTER_API_KEY"],
                "row_url": "https://api.teamorouter.com", "row_env": ["TEAMOROUTER_API_KEY"],
                "authenticated": True},
            "teamorouter-openai": {
                "cls": "TeamoRouterProfile", "base_url": "https://api.teamorouter.com/v1",
                "api_mode": "chat_completions", "headers": {"X-Client-Source": "hermes-teamorouter-plugin"},
                "env": ["TEAMOROUTER_API_KEY"], "row_url": "https://api.teamorouter.com/v1",
                "row_env": ["TEAMOROUTER_API_KEY"], "authenticated": True},
        })

    def test_pickers_and_context_windows_come_from_the_catalog(self):
        fixture = str(TESTS_DIR / "pricing_fixture.json")
        found = self._python(f"""
import io, json, urllib.request
from providers import get_provider_profile
from agent.model_metadata import get_model_context_length
seen = []
class _Resp(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *a): return False
def fake_urlopen(req, timeout=None):
    seen.append({{"url": req.full_url, "headers": {{k.lower(): v for k, v in req.header_items()}}}})
    return _Resp(open({fixture!r}, "rb").read())
urllib.request.urlopen = fake_urlopen  # test harness only: stands in for the network
claude = get_provider_profile("teamorouter")
other = get_provider_profile("teamorouter-openai")
out = {{"claude": claude.fetch_models(api_key="{API_KEY}", base_url=claude.base_url),
       "openai": other.fetch_models(api_key="{API_KEY}", base_url=other.base_url),
       "context": {{m: get_model_context_length(m, base_url=p.base_url, api_key="{API_KEY}", provider=p.name)
                   for m, p in (("claude-haiku-4-5", claude), ("grok-4.6", other))}},
       "seen": seen}}
print(json.dumps(out))
""")
        self.assertEqual(found["claude"], EXPECTED_CLAUDE)
        self.assertEqual(found["openai"], EXPECTED_OPENAI)
        self.assertEqual(found["context"], {"claude-haiku-4-5": 200000, "grok-4.6": 500000})
        self.assertEqual([s["url"] for s in found["seen"]], ["https://api.teamorouter.com/v1/models/pricing"])
        self.assertNotIn("authorization", found["seen"][0]["headers"])

    def test_claude_turn_uses_anthropic_messages_with_prompt_caching(self):
        server = FakeMessagesServer()
        try:
            self._turn("teamorouter", "claude-sonnet-5-5", server.base_url)
        finally:
            server.close()
        main = [r for r in server.requests if r["body"].get("tools")]
        self.assertTrue(main, "the turn made no Messages request")
        first = main[0]
        self.assertEqual(first["path"].split("?", 1)[0], "/v1/messages")
        self.assertEqual(first["body"]["model"], "claude-sonnet-5-5")
        self.assertEqual(first["headers"].get("x-api-key"), API_KEY)
        self.assertNotIn("authorization", first["headers"])
        self.assertIn("anthropic-version", first["headers"])
        # Hermes marks cache breakpoints for Claude on a third-party Anthropic gateway.
        self.assertIn('"cache_control"', json.dumps(first["body"]))
        # The README says Claude requests carry the Anthropic SDK's own User-Agent
        # and no header from this plugin; keep that true.
        self.assertTrue(first["headers"].get("user-agent", "").startswith("Anthropic/Python"),
                        first["headers"].get("user-agent"))
        self.assertNotIn(SOURCE_HEADER[0], first["headers"])

    def test_openai_turn_sends_the_key_model_and_source_header(self):
        fake = _fake_llm_module()
        with fake.FakeLLMServer([fake.Text("pong")], api_key=API_KEY) as llm:
            self._turn("teamorouter-openai", "gpt-5.5", llm.base_url)
            posts = [r for r in llm.requests if r["kind"] == "main"]
        self.assertTrue(posts, "the turn made no chat request")
        first = posts[0]
        self.assertEqual(first["body"]["model"], "gpt-5.5")
        self.assertTrue(first["body"].get("tools"), "Hermes sent no tool schemas")
        self.assertEqual(first["auth"], f"Bearer {API_KEY}")
        self.assertEqual(first["headers"].get(SOURCE_HEADER[0]), SOURCE_HEADER[1])


def _fake_llm_module():
    path = HERMES_ROOT / "tests" / "fakes" / "fake_llm_provider.py"
    if not path.is_file():
        raise unittest.SkipTest("this Hermes checkout has no tests/fakes/fake_llm_provider.py")
    spec = importlib.util.spec_from_file_location("hermes_fake_llm_provider", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


if __name__ == "__main__":
    unittest.main()
