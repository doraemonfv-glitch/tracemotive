import { useEffect, useRef, useState } from "react";

export const ONBOARDING_COMMANDS = {
  serverInstall: 'python -m pip install "tracemotive[server]"',
  server: "tracemotive serve",
  identifiedDemo: "tracemotive demo",
  uncertainDemo: "tracemotive demo --scenario uncertain",
  openaiAgentsInstall: 'python -m pip install "tracemotive[openai-agents]"',
  openaiAgentsSnippet: `import tracemotive
from tracemotive.integrations.openai_agents import install

tracemotive.configure(
    enabled=True,
    endpoint="http://127.0.0.1:8765",
    capture_content=False,
)
install(local_only=True)`,
  genericPythonSnippet: `import tracemotive

tracemotive.configure(
    enabled=True,
    endpoint="http://127.0.0.1:8765",
    capture_content=False,
)

with tracemotive.trace("my-agent"):
    with tracemotive.span("work"):
        pass

tracemotive.flush()`,
} as const;

function CopyableCommand({ label, command }: { label: string; command: string }) {
  const [copyState, setCopyState] = useState<"idle" | "copied" | "failed">("idle");
  const fallbackRef = useRef<HTMLPreElement>(null);

  useEffect(() => {
    if (copyState === "failed") {
      fallbackRef.current?.focus();
    }
  }, [copyState]);

  const copy = async () => {
    try {
      if (navigator.clipboard === undefined || typeof navigator.clipboard.writeText !== "function") {
        throw new Error("clipboard unavailable");
      }
      await navigator.clipboard.writeText(command);
      setCopyState("copied");
    } catch {
      setCopyState("failed");
    }
  };

  return (
    <div className="onboarding-command">
      <span className="onboarding-command-label">{label}</span>
      <div className="onboarding-command-row">
        <pre ref={fallbackRef} tabIndex={0} aria-label={`${label} command`}>{command}</pre>
        <button type="button" className="secondary-button" onClick={() => void copy()} aria-label={`Copy ${label}`}>
          Copy
        </button>
      </div>
      {copyState === "copied" && <span className="onboarding-copy-status" role="status">Copied.</span>}
      {copyState === "failed" && <span className="onboarding-copy-status" role="status">Clipboard unavailable. Select the command above.</span>}
    </div>
  );
}

export function EmptyStateOnboarding() {
  return (
    <section className="onboarding-state" aria-label="Getting started with TraceMotive">
      <div className="onboarding-primary">
        <p className="eyebrow">First local comparison</p>
        <h2>See what changed in an AI agent run</h2>
        <p>TraceMotive compares AI agent executions and identifies the first behavioral divergence supported by the available evidence.</p>
        <p className="onboarding-limit">It does not claim that an observed divergence caused a failure. The collector, database, and UI stay on this machine through the loopback server. Normal installed users do not need Node.js, npm, or a repository checkout.</p>
        <CopyableCommand label="Run the identified example" command={ONBOARDING_COMMANDS.identifiedDemo} />
        <p className="onboarding-next-step">After it finishes, reload this page, select the reference and changed demo traces, and choose <strong>Compare selected traces</strong>.</p>
      </div>

      <div className="onboarding-support-grid">
        <section className="onboarding-support" aria-labelledby="onboarding-server-heading">
          <h3 id="onboarding-server-heading">Start the local server first</h3>
          <p>Run this in a terminal if the demo reports that the server is unavailable.</p>
          <CopyableCommand label="Start the loopback server" command={ONBOARDING_COMMANDS.server} />
          <p className="onboarding-muted">If the command is unavailable, install the server extra before starting it:</p>
          <CopyableCommand label="Install the server extra" command={ONBOARDING_COMMANDS.serverInstall} />
        </section>

        <section className="onboarding-support" aria-labelledby="onboarding-uncertain-heading">
          <h3 id="onboarding-uncertain-heading">See an uncertainty barrier</h3>
          <p>This example keeps repeated members unpaired when the evidence cannot establish their identity.</p>
          <CopyableCommand label="Run the uncertain example" command={ONBOARDING_COMMANDS.uncertainDemo} />
        </section>

        <section className="onboarding-support" aria-labelledby="onboarding-agent-heading">
          <h3 id="onboarding-agent-heading">Use TraceMotive with your own agent</h3>
          <p>TraceMotive itself needs no API key. Your model provider may require one, and its model traffic may leave this machine.</p>
          <p>The validated framework integration is the OpenAI Agents SDK. Generic Python is manual instrumentation, not an automatic adapter. LangGraph is not currently supported.</p>
          <CopyableCommand label="Install the OpenAI Agents extra" command={ONBOARDING_COMMANDS.openaiAgentsInstall} />
          <CopyableCommand label="OpenAI Agents public integration" command={ONBOARDING_COMMANDS.openaiAgentsSnippet} />
          <CopyableCommand label="Generic Python manual instrumentation" command={ONBOARDING_COMMANDS.genericPythonSnippet} />
        </section>
      </div>
    </section>
  );
}
