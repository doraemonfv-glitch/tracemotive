# Issue 14 OpenAI Agents example

This is the minimal Frozen Specification example. It runs one real OpenAI
Agents SDK Agent that performs an LLM call, calls a deterministic weather tool,
and performs a final LLM call. Any persisted TraceMotive Trace can be inspected
with the existing UI.

The example requires the optional `openai-agents` package, an ASGI server such
as `uvicorn`, and an `OPENAI_API_KEY` in the environment. It makes a real model
request; no credential is stored in the repository. The example does not enable
TraceMotive content capture.

From the repository root, install the local package with the server and
OpenAI integration extras:

```powershell
python -m pip install -e ".[server,openai-agents]"
```

The supported `openai-agents` range is `>=0.17,<0.18`. The Python import name
and distribution name are both `tracemotive`.

Use three terminals from the repository root:

1. Start the existing loopback-only Collector. Keep it running; its default
   SQLite repository is `:memory:` for this local example and is cleared when
   the Collector process stops.

   ```text
   python -m uvicorn tracemotive.collector:create_app --factory --host 127.0.0.1 --port 8765
   ```

2. Run the example:

   ```text
   python -m examples.openai_agents_example
   ```

   It calls `tracemotive.configure(enabled=True, capture_content=False)`, then
   installs `tracemotive.integrations.openai_agents.install(local_only=True)`.
   `local_only=True` makes TraceMotive the only OpenAI tracing processor for the
   process, so TraceMotive observability/tracing traffic uses the local TraceMotive
   path rather than preserving the OpenAI tracing exporter. This does not make
   the model request local: `Runner.run_sync` still makes a real network/API
   request to OpenAI, and model input may leave this machine.

   `capture_content=False` means TraceMotive does not capture this example's
   prompt/output content into TraceMotive events. It does not prevent the OpenAI
   Agents SDK from sending the model input required for the real model request
   to OpenAI.

3. Start the existing frontend and open the printed local URL:

   ```text
   cd frontend
   npm install
   npm run dev
   ```

   Open `http://127.0.0.1:5173`, select the new Trace if it appears, and
   inspect its Trace Detail, Span Tree, Timeline, and Span Inspector. The
   expected execution is `Agent -> LLM -> Tool -> LLM`.

If the optional SDK is missing, the example exits with an install message. If
the API key or collector is unavailable, it exits with a setup/runtime error;
it does not print request headers, client objects, or captured content.
