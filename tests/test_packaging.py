"""What ships: manifest, docs, tracked links, and what the code may touch."""

from __future__ import annotations

import ast
import re
import sys
import tomllib
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import PLUGIN_DIR, ROOT, load_plugin  # noqa: E402

SOURCES = sorted(PLUGIN_DIR.glob("*.py"))
UTM = {"utm_source": ["hermes"], "utm_medium": ["plugin"], "utm_campaign": ["hermes_catalog"]}
# Pages on teamorouter.com that do not redirect (a redirect drops the query).
ALLOWED_SITE_PATHS = {"/", "/docs/api-integration"}


def _manifest() -> dict:
    """Top-level scalars of plugin.yaml, plus requires_env names (no YAML dependency)."""
    fields: dict = {}
    text = (PLUGIN_DIR / "plugin.yaml").read_text(encoding="utf-8")
    for line in text.splitlines():
        if line and not line[0].isspace() and ":" in line:
            key, _, value = line.partition(":")
            fields[key.strip()] = value.strip().strip('"')
    fields["requires_env"] = re.findall(r"^\s+- name: (\S+)$", text, re.MULTILINE)
    return fields


def _imports(path: Path) -> set:
    names = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            names.add(node.module or "")
    return names


class ManifestTests(unittest.TestCase):
    def test_manifest_declares_a_model_provider(self):
        manifest = _manifest()
        plugin, _ = load_plugin()
        self.assertEqual(manifest["name"], "teamorouter")
        self.assertEqual(manifest["kind"], "model-provider")
        self.assertEqual(manifest["requires_env"], [plugin.API_KEY_ENV])
        self.assertEqual(manifest["requires_hermes"], ">=0.21.5")

    def test_versions_agree(self):
        plugin, _ = load_plugin()
        pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(_manifest()["version"], plugin.__version__)
        self.assertEqual(pyproject["project"]["version"], plugin.__version__)
        self.assertIn(f"### {plugin.__version__}", (ROOT / "README.md").read_text(encoding="utf-8"))

    def test_plugin_has_no_python_dependencies(self):
        pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(pyproject["project"]["dependencies"], [])
        self.assertFalse((PLUGIN_DIR / "pyproject.toml").exists())
        self.assertNotIn("python_dependencies", _manifest())


class DocsTests(unittest.TestCase):
    def test_plugin_readme_matches_repo_readme(self):
        # The catalog page renders <subdir>/README.md; GitHub renders the root one.
        self.assertEqual((PLUGIN_DIR / "README.md").read_bytes(), (ROOT / "README.md").read_bytes())

    def test_readme_links_are_absolute(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        for target in re.findall(r"\]\(([^)]+)\)", readme):
            self.assertTrue(target.startswith("https://"), target)

    def test_every_teamorouter_site_link_is_tracked_and_does_not_redirect(self):
        plugin, _ = load_plugin()
        texts = {
            "README.md": (ROOT / "README.md").read_text(encoding="utf-8"),
            "after-install.md": (PLUGIN_DIR / "after-install.md").read_text(encoding="utf-8"),
            "plugin.yaml": (PLUGIN_DIR / "plugin.yaml").read_text(encoding="utf-8"),
            "signup_url": plugin.SIGNUP_URL,
        }
        seen = 0
        for name, text in texts.items():
            for raw in re.findall(r"https://(?:www\.)?teamorouter\.com[^\s)\"'>]*", text):
                url = urlparse(raw)
                self.assertIn(url.path, ALLOWED_SITE_PATHS, f"{name}: {raw}")
                self.assertEqual(parse_qs(url.query), UTM, f"{name}: {raw}")
                seen += 1
        self.assertGreaterEqual(seen, 4)

    def test_readme_discloses_network_credentials_and_telemetry(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        for needle in (
            "TEAMOROUTER_API_KEY",
            "api.teamorouter.com/v1/models/pricing",
            "X-Client-Source: hermes-teamorouter-plugin",
            "Anthropic/Python",
            "api.teamorouter.com/v1/messages",
            "No telemetry",
            "stores no",
        ):
            self.assertIn(needle, readme)


class CodeSurfaceTests(unittest.TestCase):
    def test_the_plugin_is_one_module(self):
        self.assertEqual([p.name for p in SOURCES], ["__init__.py"])

    def test_only_the_standard_library_and_public_hermes_modules(self):
        allowed = {"__future__", "json", "logging", "threading", "time", "typing", "urllib.request",
                   "urllib.parse", "providers", "providers.base"}
        for path in SOURCES:
            self.assertLessEqual(_imports(path), allowed, path.name)

    def test_no_env_files_processes_or_dynamic_code(self):
        banned_calls = {"open", "exec", "eval", "compile", "__import__", "system", "popen", "getenv",
                        "Popen", "run", "Thread", "setattr", "delattr", "read_text", "write_text",
                        "read_bytes", "write_bytes", "unlink", "remove", "mkdir"}
        for path in SOURCES:
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Call):
                    func = node.func
                    called = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
                    self.assertNotIn(called, banned_calls, f"{path.name}:{node.lineno}")
                if isinstance(node, ast.Attribute):
                    self.assertNotEqual(node.attr, "environ", f"{path.name}:{node.lineno}")

    def test_registers_two_providers_through_register_provider_only(self):
        tree = ast.parse((PLUGIN_DIR / "__init__.py").read_text(encoding="utf-8"))
        calls = [n.func.id for n in ast.walk(tree)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id.startswith("register")]
        self.assertEqual(calls, ["register_provider", "register_provider"])

    def test_only_api_teamorouter_com_is_contacted(self):
        source = (PLUGIN_DIR / "__init__.py").read_text(encoding="utf-8")
        hosts = {urlparse(u).netloc for u in re.findall(r"https://[^\s\"')]+", source)}
        self.assertEqual(hosts - {"teamorouter.com"}, {"api.teamorouter.com"})


if __name__ == "__main__":
    unittest.main()
