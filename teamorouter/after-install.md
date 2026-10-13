# TeamoRouter providers installed

Set your key and pick a model:

    export TEAMOROUTER_API_KEY="..."
    hermes --provider teamorouter --model claude-sonnet-5-5
    hermes --provider teamorouter-openai --model gpt-6.1-sol

`teamorouter` serves Claude models over the native Anthropic Messages API, so
prompt caching and thinking work as with Anthropic. `teamorouter-openai`
serves GPT, Gemini, DeepSeek, GLM and Grok models over the OpenAI-compatible
API. Both use the same key. Or run `hermes model` and choose either provider.

Create an account and an API key at
<https://teamorouter.com/?utm_source=hermes&utm_medium=plugin&utm_campaign=hermes_catalog>;
setup steps are in the
[API integration guide](https://teamorouter.com/docs/api-integration?utm_source=hermes&utm_medium=plugin&utm_campaign=hermes_catalog).
