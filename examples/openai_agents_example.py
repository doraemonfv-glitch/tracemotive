"""Minimal Issue 14 OpenAI Agents SDK example.

This example intentionally uses the real OpenAI Agents SDK execution path:
Agent -> LLM -> Tool -> LLM.  TraceMotive remains content-off by default, and
the optional framework dependency is imported only when the example runs.
"""

from __future__ import annotations

from urllib.error import URLError
from urllib.request import urlopen

import tracemotive
import tracemotive.integrations.openai_agents


COLLECTOR_ENDPOINT = "http://127.0.0.1:8765"
UI_URL = "http://127.0.0.1:8765"


def _collector_available() -> bool:
    try:
        with urlopen(f"{COLLECTOR_ENDPOINT}/api/v1/health", timeout=1) as response:
            return getattr(response, "status", None) == 200
    except (OSError, URLError, ValueError):
        return False


def main() -> None:
    try:
        from agents import Agent, ModelSettings, Runner, function_tool
    except ImportError as exc:
        raise SystemExit(
            "This example requires the optional 'openai-agents' package. "
            "Install it in the active environment before running the example."
        ) from exc

    if not _collector_available():
        raise SystemExit(
            "The TraceMotive collector is unavailable at "
            f"{COLLECTOR_ENDPOINT}. Start it before running the example."
        )

    @function_tool
    def lookup_weather(city: str) -> str:
        """Return a deterministic, non-sensitive demo forecast."""

        if city.casefold() == "tokyo":
            return "Tokyo: clear skies, 21 degrees Celsius."
        return f"{city}: the demo forecast is unavailable."

    tracemotive.configure(
        enabled=True,
        endpoint=COLLECTOR_ENDPOINT,
        capture_content=False,
    )
    tracemotive.integrations.openai_agents.install(local_only=True)

    agent = Agent(
        name="Weather assistant",
        instructions=(
            "Answer weather questions concisely. Always call lookup_weather "
            "before answering, and use its result in your response."
        ),
        tools=[lookup_weather],
        model_settings=ModelSettings(tool_choice="required"),
        reset_tool_choice=True,
        tool_use_behavior="run_llm_again",
    )

    try:
        Runner.run_sync(agent, "What is the weather in Tokyo?")
    except Exception as exc:
        raise SystemExit(
            "The Agent run failed. Check that OPENAI_API_KEY is available and "
            "the local TraceMotive collector is running."
        ) from exc
    finally:
        flushed = tracemotive.flush()

    if flushed:
        print(
            "TraceMotive example finished; events reached a terminal transport "
            f"outcome. Open {UI_URL} to inspect any persisted Trace."
        )
    else:
        print(
            "Agent run finished, but TraceMotive could not flush its local "
            f"events. Check the collector at {COLLECTOR_ENDPOINT}."
        )


if __name__ == "__main__":
    main()
