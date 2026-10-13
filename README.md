# hermes-provider-teamorouter

[TeamoRouter](https://teamorouter.com/?utm_source=hermes&utm_medium=plugin&utm_campaign=hermes_catalog)
model providers for [Hermes Agent](https://github.com/NousResearch/hermes-agent).

TeamoRouter is an API gateway focused on Claude models, and it also serves GPT,
Gemini, DeepSeek, GLM and Grok models behind one API key. Hermes speaks one wire
protocol per provider, so this plugin registers two providers on the same key:

| Provider | Models | Wire protocol | Endpoint |
|---|---|---|---|
| `teamorouter` | Claude (`claude-*`) | Native Anthropic Messages API | `https://api.teamorouter.com/v1/messages` |
| `teamorouter-openai` | GPT, Gemini, DeepSeek, GLM, Grok | OpenAI-compatible Chat Completions | `https://api.teamorouter.com/v1` |

Claude goes over the Anthropic protocol so Hermes applies Anthropic prompt
caching (cache breakpoints) and thinking (`agent.reasoning_effort`) exactly as
it does for Anthropic itself.

The plugin registers these two model providers and nothing else: no tools,
hooks, middleware, commands, background threads or processes. Hermes core files
are not touched.

Maintained by TeamoRouter.

## What you get

- `--provider teamorouter` / `--provider teamorouter-openai`, both providers in
  `hermes model` and `/model`, and `hermes doctor` checks, all wired by Hermes
  from the provider profiles.
- `/model` pickers built from TeamoRouter's public catalog: Claude models under
  `teamorouter`, the other tool-capable chat models under `teamorouter-openai`.
  Image models, per-image billing, service-tier and free-tier variants, and
  models under 64K context are left out. When the catalog is unreachable a short
  built-in list is shown instead.
- Context windows from the same catalog, so Hermes does not fall back to its
  generic default for TeamoRouter models.
- Cheap models for Hermes side tasks (compression, titles): `claude-haiku-4-5`
  on `teamorouter`, `gemini-3.5-flash-lite` on `teamorouter-openai`.

## Requirements

- Hermes Agent 0.21.5 or newer. The `teamorouter` provider uses Hermes'
  Anthropic transport, which needs the `anthropic` Python package; Hermes
  installs it on first use (or ships it with its `anthropic` extra).
- A TeamoRouter API key in `TEAMOROUTER_API_KEY`.
  Create an account at
  [teamorouter.com](https://teamorouter.com/?utm_source=hermes&utm_medium=plugin&utm_campaign=hermes_catalog);
  key setup is described in the
  [API integration guide](https://teamorouter.com/docs/api-integration?utm_source=hermes&utm_medium=plugin&utm_campaign=hermes_catalog).
- The plugin itself has no Python dependencies beyond the standard library.

## Install

From the Hermes plugin catalog (once listed):

```bash
hermes plugins install teamorouter
```

Or drop the plugin directory into your Hermes home:

```bash
git clone https://github.com/2867297362mjb-sketch/hermes-provider-teamorouter.git
mkdir -p ~/.hermes/plugins/model-providers
cp -r hermes-provider-teamorouter/teamorouter ~/.hermes/plugins/model-providers/teamorouter
```

## Use

```bash
export TEAMOROUTER_API_KEY="..."
hermes --provider teamorouter --model claude-sonnet-5-5
hermes --provider teamorouter-openai --model gpt-6.1-sol
```

Or run `hermes model`, pick TeamoRouter or TeamoRouter (OpenAI-compatible), and
choose a model. `hermes setup` also offers `TEAMOROUTER_API_KEY`.

If you point `model.base_url` at your own proxy, the picker comes from Hermes'
generic `{base_url}/models` request instead of TeamoRouter's catalog.

## Network calls, credentials and telemetry

- **Chat requests** go to TeamoRouter through Hermes' own clients with the key
  from `TEAMOROUTER_API_KEY`: Claude requests to
  `https://api.teamorouter.com/v1/messages` (key in the `x-api-key` header),
  other models to `https://api.teamorouter.com/v1/chat/completions` (key as
  `Authorization: Bearer`). Hermes reads the key; the plugin never reads,
  stores, logs or forwards it.
- **Model catalog**: the plugin itself makes one kind of request,
  `GET https://api.teamorouter.com/v1/models/pricing`, a public endpoint. It is
  sent **without** the API key (User-Agent `hermes-provider-teamorouter/<version>`),
  cached in memory for an hour, and used only to build the pickers and report
  context windows. A failed fetch is retried at most every five minutes.
- **Source header** (`teamorouter-openai` only): its requests carry a fixed
  `X-Client-Source: hermes-teamorouter-plugin` header, which Hermes sends from
  the provider profile. It holds no user, machine or session data, and
  TeamoRouter uses it only to count requests that come from this plugin.
  Hermes does not send profile headers on its Anthropic transport, so Claude
  requests (`teamorouter`) carry no header from this plugin; they go out with
  the Anthropic SDK's own User-Agent (`Anthropic/Python <sdk version>`), which
  does not identify Hermes or this plugin.
- **No telemetry.** The plugin sends no analytics, usage reports or crash data
  anywhere, and its own code contacts no host other than `api.teamorouter.com`.
- **No files, no processes.** The plugin reads and writes no files, stores no
  credentials and starts no threads or processes.
- Prompts, tool schemas and model output are processed by TeamoRouter and the
  upstream model vendor serving the chosen model. See TeamoRouter's terms on
  [teamorouter.com](https://teamorouter.com/?utm_source=hermes&utm_medium=plugin&utm_campaign=hermes_catalog).

## Development

```bash
python3 -m unittest discover -s tests -v
```

The unit tests use stub `providers` modules and need neither Hermes nor the
network. `tests/test_hermes_integration.py` runs only when Hermes is importable
(a Hermes checkout's interpreter, with `PYTHONPATH` pointing at it and its
`anthropic` extra installed). It installs the plugin into a temporary
`HERMES_HOME` and drives real Hermes turns against local fake servers, one per
wire protocol, so it makes no real network calls either. Check the plugin the
way the catalog CI does:

```bash
hermes plugins validate ./teamorouter --install-deps
```

## Changelog

### 0.1.0

First release: `teamorouter` (Claude, Anthropic Messages) and
`teamorouter-openai` (OpenAI-compatible) providers, catalog-backed pickers and
context windows, `X-Client-Source` header on the OpenAI-compatible provider.

## License

MIT. See [LICENSE](https://github.com/2867297362mjb-sketch/hermes-provider-teamorouter/blob/main/LICENSE).
