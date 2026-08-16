# OpenAI Agents SDK integration

TraceMotive v0.3 supports the OpenAI Agents SDK range `>=0.17,<0.18`. The
package extra is optional; core `import tracemotive` does not import or require
the `agents` package. The range was compatibility-tested at 0.17.0, 0.17.4,
and 0.17.8 for the adapter callbacks, span-data fields, processor registration
functions, and the Issue 14 Agent construction surface.

From a TraceMotive checkout, install the optional integration with:

```text
python -m pip install -e ".[openai-agents]"
```

Install the optional OpenAI Agents SDK, then register the TraceMotive Issue 08
processor explicitly:

```python
from tracemotive.integrations.openai_agents import install

install(local_only=True)
```

`local_only=True` is the default and replaces the OpenAI Agents SDK global
processor list with the TraceMotive processor only. Existing OpenAI and
third-party processors therefore do not receive subsequent traces.

Use `local_only=False` to add TraceMotive after the existing processors. Existing
processors remain active, so another configured processor may still export the
same framework trace remotely.

Installation is process-global. Repeating the same mode is a no-op; changing
the mode later raises `tracemotive.TraceMotiveConfigurationError`. The mode cannot
be switched dynamically, and v0.2 does not provide uninstall or restore.

Installing the integration does not enable TraceMotive. Configure the core SDK
separately if tracing should produce TraceMotive events. Without the optional
`openai-agents` package, importing `tracemotive` remains valid, while calling
this integration boundary raises a deterministic configuration error. The
public distribution name is `tracemotive`; public package-index publication is
outside this local onboarding procedure.
