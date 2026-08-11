# OpenAI Agents SDK integration

Install the optional OpenAI Agents SDK, then register the AgentLens Issue 08
processor explicitly:

```python
from agentlens.integrations.openai_agents import install

install(local_only=True)
```

`local_only=True` is the default and replaces the OpenAI Agents SDK global
processor list with the AgentLens processor only. Existing OpenAI and
third-party processors therefore do not receive subsequent traces.

Use `local_only=False` to add AgentLens after the existing processors. Existing
processors remain active, so another configured processor may still export the
same framework trace remotely.

Installation is process-global. Repeating the same mode is a no-op; changing
the mode later raises `agentlens.AgentLensConfigurationError`. The mode cannot
be switched dynamically, and v0.1 does not provide uninstall or restore.

Installing the integration does not enable AgentLens. Configure the core SDK
separately if tracing should produce AgentLens events. Without the optional
`openai-agents` package, importing `agentlens` remains valid, while calling
this integration boundary raises a deterministic configuration error.
