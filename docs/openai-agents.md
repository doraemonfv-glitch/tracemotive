# OpenAI Agents SDK integration

TraceMotive v0.5.0 supports the OpenAI Agents SDK range `>=0.17,<0.18`. The
package extra is optional; core `import tracemotive` does not import or require
the `agents` package. The range was compatibility-tested at 0.17.0, 0.17.4,
and 0.17.8 for the adapter callbacks, span-data fields, processor registration
functions, and the Issue 14 Agent construction surface.

## Installed-user path

Normal installed users do not need a repository checkout, an editable install,
or repository example modules.

Install the optional extra:

```text
python -m pip install "tracemotive[openai-agents]"
```

With a local Collector already running (`tracemotive serve`), configure
TraceMotive and register the public integration:

```python
import tracemotive
from tracemotive.integrations.openai_agents import install

tracemotive.configure(
    enabled=True,
    endpoint="http://127.0.0.1:8765",
    capture_content=False,
)
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
be switched dynamically, and the current integration does not provide uninstall
or restore.

Installing the integration does not enable TraceMotive. Configure the core SDK
separately if tracing should produce TraceMotive events. Without the optional
`openai-agents` package, importing `tracemotive` remains valid, while calling
this integration boundary raises a deterministic configuration error.

`local_only=True` controls framework tracing processors. It does not make model
traffic local. A provider request may still leave the machine.

## Development from a repository checkout

Contributor and source-development work may install the extra from a checkout:

```text
python -m pip install -e ".[openai-agents]"
```

The repository example at `examples/openai_agents_example.py` is checkout-only.
It is not part of a normal installed package and is not an installed-user
command.

## Other integrations

Generic Python support is the public `configure`, `trace`, `span`, and `flush`
SDK. That path is manual instrumentation, not an automatic framework adapter.

LangGraph is not currently supported.
