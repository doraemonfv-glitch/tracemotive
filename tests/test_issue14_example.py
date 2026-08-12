import importlib
import inspect
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import ModuleType
import unittest
from unittest.mock import patch


class Issue14ExampleTests(unittest.TestCase):
    def test_example_module_imports_without_optional_agents_sdk(self):
        example = importlib.import_module("examples.openai_agents_example")

        self.assertTrue(callable(example.main))

    def test_example_reports_missing_optional_dependency_clearly(self):
        example = importlib.import_module("examples.openai_agents_example")

        with patch.dict("sys.modules", {"agents": None}):
            with self.assertRaisesRegex(SystemExit, "requires the optional"):
                example.main()

    def test_example_reports_collector_unavailable_clearly(self):
        example = importlib.import_module("examples.openai_agents_example")
        fake_agents = ModuleType("agents")
        fake_agents.Agent = object
        fake_agents.ModelSettings = object
        fake_agents.Runner = object
        fake_agents.function_tool = object

        with (
            patch.dict("sys.modules", {"agents": fake_agents}),
            patch.object(example, "_collector_available", return_value=False),
        ):
            with self.assertRaisesRegex(SystemExit, "collector is unavailable"):
                example.main()

    def test_example_uses_public_tracemotive_wiring_only(self):
        example = importlib.import_module("examples.openai_agents_example")
        source = inspect.getsource(example)

        self.assertIn("tracemotive.configure", source)
        self.assertIn(
            "tracemotive.integrations.openai_agents.install(local_only=True)",
            source,
        )
        self.assertIn("capture_content=False", source)
        self.assertIn("Runner.run_sync", source)
        self.assertIn("@function_tool", source)
        for forbidden in (
            "OpenAITracingProcessor",
            "LocalTransport",
            "Repository",
            "create_app",
            "_reset_for_tests",
            "_set_event_sink",
        ):
            self.assertNotIn(forbidden, source)
        self.assertNotIn("os.environ", source)
        self.assertNotIn("api_key=", source.casefold())

    def test_example_builds_agent_and_runs_deterministic_tool_boundary(self):
        example = importlib.import_module("examples.openai_agents_example")
        observed = {}

        class FakeAgent:
            def __init__(self, **kwargs):
                observed["agent"] = kwargs
                self.tools = kwargs["tools"]

        class FakeRunner:
            @staticmethod
            def run_sync(agent, prompt):
                observed["prompt"] = prompt
                tool = agent.tools[0]
                observed["tool_result"] = tool("Tokyo")

        class FakeModelSettings:
            def __init__(self, *, tool_choice):
                self.tool_choice = tool_choice

        def fake_function_tool(function):
            return function

        fake_agents = ModuleType("agents")
        fake_agents.Agent = FakeAgent
        fake_agents.ModelSettings = FakeModelSettings
        fake_agents.Runner = FakeRunner
        fake_agents.function_tool = fake_function_tool

        with (
            patch.dict("sys.modules", {"agents": fake_agents}),
            patch.object(example, "_collector_available", return_value=True) as collector_available,
            patch.object(example.tracemotive, "configure") as configure,
            patch.object(example.tracemotive.integrations.openai_agents, "install") as install,
            patch.object(example.tracemotive, "flush", return_value=True) as flush,
        ):
            example.main()

        collector_available.assert_called_once_with()
        configure.assert_called_once_with(
            enabled=True,
            endpoint=example.COLLECTOR_ENDPOINT,
            capture_content=False,
        )
        install.assert_called_once_with(local_only=True)
        flush.assert_called_once_with()
        self.assertEqual(observed["agent"]["name"], "Weather assistant")
        self.assertEqual(
            observed["agent"]["model_settings"].tool_choice,
            "required",
        )
        self.assertTrue(observed["agent"]["reset_tool_choice"])
        self.assertEqual(
            observed["agent"]["tool_use_behavior"],
            "run_llm_again",
        )
        self.assertEqual(observed["prompt"], "What is the weather in Tokyo?")
        self.assertEqual(
            observed["tool_result"],
            "Tokyo: clear skies, 21 degrees Celsius.",
        )

    def test_flush_true_does_not_claim_trace_persistence(self):
        example = importlib.import_module("examples.openai_agents_example")
        fake_agents = ModuleType("agents")

        class FakeAgent:
            def __init__(self, **kwargs):
                self.tools = kwargs["tools"]

        class FakeModelSettings:
            def __init__(self, *, tool_choice):
                self.tool_choice = tool_choice

        class FakeRunner:
            @staticmethod
            def run_sync(agent, prompt):
                del agent, prompt

        fake_agents.Agent = FakeAgent
        fake_agents.ModelSettings = FakeModelSettings
        fake_agents.Runner = FakeRunner
        fake_agents.function_tool = lambda function: function
        output = StringIO()

        with (
            patch.dict("sys.modules", {"agents": fake_agents}),
            patch.object(example, "_collector_available", return_value=True),
            patch.object(example.tracemotive, "configure"),
            patch.object(example.tracemotive.integrations.openai_agents, "install"),
            patch.object(example.tracemotive, "flush", return_value=True),
            redirect_stdout(output),
        ):
            example.main()

        text = output.getvalue()
        self.assertIn("terminal transport outcome", text)
        self.assertIn("any persisted Trace", text)
        self.assertNotIn("Inspect the trace at", text)
        self.assertNotIn("the trace was persisted", text.casefold())

    def test_flush_false_keeps_delivery_warning(self):
        example = importlib.import_module("examples.openai_agents_example")
        fake_agents = ModuleType("agents")

        class FakeAgent:
            def __init__(self, **kwargs):
                self.tools = kwargs["tools"]

        class FakeModelSettings:
            def __init__(self, *, tool_choice):
                self.tool_choice = tool_choice

        class FakeRunner:
            @staticmethod
            def run_sync(agent, prompt):
                del agent, prompt

        fake_agents.Agent = FakeAgent
        fake_agents.ModelSettings = FakeModelSettings
        fake_agents.Runner = FakeRunner
        fake_agents.function_tool = lambda function: function
        output = StringIO()

        with (
            patch.dict("sys.modules", {"agents": fake_agents}),
            patch.object(example, "_collector_available", return_value=True),
            patch.object(example.tracemotive, "configure"),
            patch.object(example.tracemotive.integrations.openai_agents, "install"),
            patch.object(example.tracemotive, "flush", return_value=False),
            redirect_stdout(output),
        ):
            example.main()

        text = output.getvalue()
        self.assertIn("could not flush its local events", text)
        self.assertIn(example.COLLECTOR_ENDPOINT, text)

    def test_example_readme_documents_the_frozen_workflow(self):
        readme = (
            Path(__file__).resolve().parents[1] / "examples" / "README.md"
        ).read_text(encoding="utf-8")
        normalized_readme = " ".join(readme.split())

        for required in (
            "tracemotive.collector:create_app",
            "python -m examples.openai_agents_example",
            "tracemotive.configure(enabled=True, capture_content=False)",
            "tracemotive.integrations.openai_agents.install(local_only=True)",
            "TraceMotive observability/tracing traffic",
            "real network/API",
            "model input may leave this machine",
            "does not capture this example's",
            "does not prevent the OpenAI Agents SDK",
            "http://127.0.0.1:5173",
            "Agent -> LLM -> Tool -> LLM",
        ):
            self.assertIn(required, normalized_readme)


if __name__ == "__main__":
    unittest.main()
