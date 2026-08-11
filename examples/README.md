# Issue 14 OpenAI Agents example

This is the minimal Frozen Specification example. It runs one real OpenAI
Agents SDK Agent that performs an LLM call, calls a deterministic weather tool,
and performs a final LLM call. Any persisted AgentLens Trace can be inspected
with the existing UI.

The example requires the optional `openai-agents` package, an ASGI server such
as `uvicorn`, and an `OPENAI_API_KEY` in the environment. It makes a real model
request; no credential is stored in the repository. The example does not enable
AgentLens content capture.

Install the example-only runtime dependencies in the active environment if
needed:

```text
python -m pip install openai-agents uvicorn
```

Use three terminals from the repository root:

1. Start the existing loopback-only Collector. Keep it running; its default
   repository is in memory for this local example.

   ```text
   python -m uvicorn agentlens.collector:create_app --factory --host 127.0.0.1 --port 8765
   ```

2. Run the example:

   ```text
   python -m examples.openai_agents_example
   ```

   It calls `agentlens.configure(enabled=True, capture_content=False)`, then
   installs `agentlens.integrations.openai_agents.install(local_only=True)`.
   `local_only=True` makes AgentLens the only OpenAI tracing processor for the
   process, so AgentLens observability/tracing traffic uses the local AgentLens
   path rather than preserving the OpenAI tracing exporter. This does not make
   the model request local: `Runner.run_sync` still makes a real network/API
   request to OpenAI, and model input may leave this machine.

   `capture_content=False` means AgentLens does not capture this example's
   prompt/output content into AgentLens events. It does not prevent the OpenAI
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
