import os
from pathlib import Path
import subprocess
import sys
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import agentlens
from agentlens.integrations import openai_agents


class _Processor:
    def __init__(self, name):
        self.name = name
        self.callbacks = []

    def on_trace_start(self, trace):
        self.callbacks.append(("trace", trace))

    def on_span_start(self, span):
        self.callbacks.append(("span", span))

    def __repr__(self):
        return f"_Processor({self.name!r})"


class _FakeAgents:
    def __init__(self):
        self._processors = ()
        self.calls = []
        self.fail_next = None
        self.registration_started = threading.Event()
        self.release_registration = threading.Event()
        self.pause_registration = False

    @property
    def processors(self):
        return list(self._processors)

    @processors.setter
    def processors(self, processors):
        self._processors = tuple(processors)

    def _before_commit(self):
        if self.pause_registration:
            self.registration_started.set()
            self.release_registration.wait(timeout=5)

    def add_trace_processor(self, processor):
        self.calls.append(("add", processor))
        self._before_commit()
        if self.fail_next == "add":
            self.fail_next = None
            raise RuntimeError("add failed")
        self.processors = (*self._processors, processor)

    def set_trace_processors(self, processors):
        self.calls.append(("set", tuple(processors)))
        self._before_commit()
        if self.fail_next == "set":
            self.fail_next = None
            raise RuntimeError("set failed")
        self.processors = processors

    def deliver_callbacks(self, trace, span):
        for processor in self.processors:
            processor.on_trace_start(trace)
            processor.on_span_start(span)


class OpenAIInstallationTests(unittest.TestCase):
    def setUp(self):
        self.fake_agents = _FakeAgents()
        openai_agents._reset_installation_for_tests()
        self.agents_patch = patch.object(openai_agents, "_agents", self.fake_agents)
        self.agents_patch.start()

    def tearDown(self):
        openai_agents._reset_installation_for_tests()
        self.fake_agents.set_trace_processors([])
        self.agents_patch.stop()

    def test_local_only_true_replaces_existing_processors(self):
        first = _Processor("P1")
        second = _Processor("P2")
        self.fake_agents.processors = [first, second]

        openai_agents.install(local_only=True)

        self.assertEqual(len(self.fake_agents.processors), 1)
        self.assertIs(self.fake_agents.processors[0], openai_agents._installed_processor)
        self.assertIsInstance(
            self.fake_agents.processors[0], openai_agents.AgentLensOpenAIProcessor
        )
        self.assertEqual([call[0] for call in self.fake_agents.calls], ["set"])

    def test_local_only_false_adds_after_existing_processors(self):
        first = _Processor("P1")
        second = _Processor("P2")
        self.fake_agents.processors = [first, second]

        openai_agents.install(local_only=False)

        self.assertEqual(self.fake_agents.processors[:2], [first, second])
        self.assertIsInstance(
            self.fake_agents.processors[2], openai_agents.AgentLensOpenAIProcessor
        )
        self.assertEqual([call[0] for call in self.fake_agents.calls], ["add"])

    def test_repeated_same_mode_is_a_noop_and_reuses_processor(self):
        openai_agents.install(local_only=True)
        installed = openai_agents._installed_processor

        openai_agents.install(local_only=True)

        self.assertIs(openai_agents._installed_processor, installed)
        self.assertEqual(len(self.fake_agents.processors), 1)
        self.assertEqual(len(self.fake_agents.calls), 1)

    def test_repeated_additive_install_is_a_noop(self):
        openai_agents.install(local_only=False)
        installed = openai_agents._installed_processor

        openai_agents.install(local_only=False)

        self.assertIs(openai_agents._installed_processor, installed)
        self.assertEqual(self.fake_agents.processors, [installed])
        self.assertEqual(len(self.fake_agents.calls), 1)

    def test_mode_change_rejects_without_mutating_configuration(self):
        openai_agents.install(local_only=True)
        before = list(self.fake_agents.processors)
        calls = list(self.fake_agents.calls)

        with self.assertRaisesRegex(
            agentlens.AgentLensConfigurationError,
            "already installed with a different local_only mode",
        ):
            openai_agents.install(local_only=False)

        self.assertEqual(self.fake_agents.processors, before)
        self.assertEqual(self.fake_agents.calls, calls)

    def test_reverse_mode_change_rejects_without_mutating_configuration(self):
        openai_agents.install(local_only=False)
        before = list(self.fake_agents.processors)
        calls = list(self.fake_agents.calls)

        with self.assertRaises(agentlens.AgentLensConfigurationError):
            openai_agents.install(local_only=True)

        self.assertEqual(self.fake_agents.processors, before)
        self.assertEqual(self.fake_agents.calls, calls)

    def test_same_mode_concurrent_install_has_one_effective_registration(self):
        barrier = threading.Barrier(16)

        def install_one():
            barrier.wait()
            openai_agents.install(local_only=False)

        with ThreadPoolExecutor(max_workers=16) as pool:
            list(pool.map(lambda _: install_one(), range(16)))

        self.assertEqual(len(self.fake_agents.calls), 1)
        self.assertEqual(len(self.fake_agents.processors), 1)
        self.assertIs(self.fake_agents.processors[0], openai_agents._installed_processor)

    def test_same_mode_true_concurrent_install_has_one_effective_registration(self):
        first = _Processor("P1")
        second = _Processor("P2")
        self.fake_agents.processors = [first, second]
        barrier = threading.Barrier(24)

        def install_one():
            barrier.wait()
            try:
                openai_agents.install(local_only=True)
            except Exception as error:
                return error
            return None

        with ThreadPoolExecutor(max_workers=24) as pool:
            outcomes = list(pool.map(lambda _: install_one(), range(24)))

        self.assertEqual(outcomes, [None] * 24)
        self.assertEqual([call[0] for call in self.fake_agents.calls], ["set"])
        self.assertEqual(openai_agents._installation_mode, True)
        self.assertEqual(len(self.fake_agents.processors), 1)
        self.assertEqual(
            sum(
                isinstance(processor, openai_agents.AgentLensOpenAIProcessor)
                for processor in self.fake_agents.processors
            ),
            1,
        )

    def test_different_mode_concurrent_install_has_one_winner(self):
        barrier = threading.Barrier(2)
        outcomes = []
        outcome_lock = threading.Lock()

        def install_one(mode):
            barrier.wait()
            try:
                openai_agents.install(local_only=mode)
            except Exception as error:
                with outcome_lock:
                    outcomes.append(error)
            else:
                with outcome_lock:
                    outcomes.append(None)

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(install_one, mode) for mode in (True, False)]
            for future in futures:
                future.result()

        self.assertEqual(outcomes.count(None), 1)
        self.assertEqual(len(outcomes), 2)
        self.assertIsInstance(
            next(outcome for outcome in outcomes if outcome is not None),
            agentlens.AgentLensConfigurationError,
        )
        self.assertEqual(len(self.fake_agents.processors), 1)
        self.assertEqual(
            openai_agents._installation_mode,
            self.fake_agents.calls[0][0] == "set",
        )

    def test_repeated_opposite_mode_races_have_one_atomic_winner(self):
        first = _Processor("P1")
        second = _Processor("P2")

        for _ in range(64):
            openai_agents._reset_installation_for_tests()
            self.fake_agents.calls.clear()
            self.fake_agents.processors = [first, second]
            barrier = threading.Barrier(2)

            def install_one(mode):
                barrier.wait()
                try:
                    openai_agents.install(local_only=mode)
                except Exception as error:
                    return mode, error
                return mode, None

            with ThreadPoolExecutor(max_workers=2) as pool:
                outcomes = list(pool.map(install_one, (True, False)))

            winners = [mode for mode, error in outcomes if error is None]
            failures = [error for _, error in outcomes if error is not None]
            self.assertEqual(len(winners), 1)
            self.assertEqual(len(failures), 1)
            self.assertIsInstance(failures[0], agentlens.AgentLensConfigurationError)
            self.assertEqual(len(self.fake_agents.calls), 1)
            self.assertEqual(openai_agents._installation_mode, winners[0])

            installed = [
                processor
                for processor in self.fake_agents.processors
                if isinstance(processor, openai_agents.AgentLensOpenAIProcessor)
            ]
            self.assertEqual(len(installed), 1)
            if winners[0]:
                self.assertEqual(self.fake_agents.calls[0][0], "set")
                self.assertEqual(self.fake_agents.processors, installed)
            else:
                self.assertEqual(self.fake_agents.calls[0][0], "add")
                self.assertEqual(self.fake_agents.processors[:2], [first, second])
                self.assertEqual(len(self.fake_agents.processors), 3)

    def test_callbacks_see_complete_pre_or_post_registration_collection(self):
        first = _Processor("P1")
        second = _Processor("P2")
        self.fake_agents.processors = [first, second]
        self.fake_agents.pause_registration = True

        worker = threading.Thread(target=openai_agents.install, kwargs={"local_only": True})
        worker.start()
        self.assertTrue(self.fake_agents.registration_started.wait(timeout=5))
        self.assertEqual(self.fake_agents.processors, [first, second])

        self.fake_agents.release_registration.set()
        worker.join(timeout=5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(self.fake_agents.processors), 1)
        self.assertIsInstance(
            self.fake_agents.processors[0], openai_agents.AgentLensOpenAIProcessor
        )

    def test_replacement_is_observable_through_callback_delivery(self):
        first = _Processor("P1")
        second = _Processor("P2")
        self.fake_agents.processors = [first, second]
        openai_agents.install(local_only=True)
        installed = openai_agents._installed_processor

        with patch.object(installed, "on_trace_start", wraps=installed.on_trace_start) as trace_start, patch.object(
            installed, "on_span_start", wraps=installed.on_span_start
        ) as span_start:
            self.fake_agents.deliver_callbacks("trace-1", "span-1")

        self.assertEqual(first.callbacks, [])
        self.assertEqual(second.callbacks, [])
        self.assertEqual(trace_start.call_count, 1)
        self.assertEqual(span_start.call_count, 1)

    def test_additive_is_observable_through_callback_delivery_without_duplicates(self):
        first = _Processor("P1")
        second = _Processor("P2")
        self.fake_agents.processors = [first, second]
        openai_agents.install(local_only=False)
        installed = openai_agents._installed_processor

        with patch.object(installed, "on_trace_start", wraps=installed.on_trace_start) as trace_start, patch.object(
            installed, "on_span_start", wraps=installed.on_span_start
        ) as span_start:
            self.fake_agents.deliver_callbacks("trace-1", "span-1")
            openai_agents.install(local_only=False)
            self.fake_agents.deliver_callbacks("trace-2", "span-2")

        self.assertEqual(first.callbacks, [("trace", "trace-1"), ("span", "span-1"), ("trace", "trace-2"), ("span", "span-2")])
        self.assertEqual(second.callbacks, first.callbacks)
        self.assertEqual(trace_start.call_count, 2)
        self.assertEqual(span_start.call_count, 2)
        self.assertEqual(
            sum(
                isinstance(processor, openai_agents.AgentLensOpenAIProcessor)
                for processor in self.fake_agents.processors
            ),
            1,
        )

    def test_registration_failure_does_not_publish_half_installed_state_and_retry_works(self):
        existing = _Processor("existing")
        self.fake_agents.processors = [existing]
        self.fake_agents.fail_next = "set"

        with self.assertRaisesRegex(RuntimeError, "set failed"):
            openai_agents.install(local_only=True)

        self.assertIsNone(openai_agents._installation_mode)
        self.assertIsNone(openai_agents._installed_processor)
        self.assertEqual(self.fake_agents.processors, [existing])

        openai_agents.install(local_only=True)
        self.assertIsNotNone(openai_agents._installed_processor)
        self.assertEqual(len(self.fake_agents.processors), 1)

    def test_additive_registration_failure_is_retryable_and_idempotent(self):
        existing = _Processor("existing")
        self.fake_agents.processors = [existing]
        self.fake_agents.fail_next = "add"

        with self.assertRaisesRegex(RuntimeError, "add failed"):
            openai_agents.install(local_only=False)

        self.assertIsNone(openai_agents._installation_mode)
        self.assertIsNone(openai_agents._installed_processor)
        self.assertEqual(self.fake_agents.processors, [existing])

        openai_agents.install(local_only=False)
        installed = openai_agents._installed_processor
        self.assertEqual(openai_agents._installation_mode, False)
        self.assertIsNotNone(installed)
        self.assertEqual(self.fake_agents.processors, [existing, installed])
        self.assertEqual([call[0] for call in self.fake_agents.calls], ["add", "add"])

        openai_agents.install(local_only=False)
        self.assertIs(openai_agents._installed_processor, installed)
        self.assertEqual([call[0] for call in self.fake_agents.calls], ["add", "add"])

    def test_invalid_mode_is_rejected_before_framework_registration(self):
        with self.assertRaisesRegex(
            agentlens.AgentLensConfigurationError,
            "local_only must be a boolean",
        ):
            openai_agents.install(local_only=1)
        self.assertEqual(self.fake_agents.calls, [])

    def test_missing_optional_dependency_fails_only_at_install_boundary(self):
        with patch.object(openai_agents, "_agents", None):
            import agentlens as imported_core

            self.assertIs(imported_core, agentlens)
            with self.assertRaisesRegex(
                agentlens.AgentLensConfigurationError,
                "openai-agents is required",
            ):
                openai_agents.install()

    def test_optional_dependency_absence_is_stable_in_a_fresh_process(self):
        environment = os.environ.copy()
        environment.pop("PYTHONPATH", None)
        environment.pop("PYTHONHOME", None)
        code = """
import agentlens
from agentlens.integrations import openai_agents
assert openai_agents._agents is None
assert openai_agents._installation_mode is None
assert openai_agents._installed_processor is None

def assert_install_fails_without_publishing_state():
    try:
        openai_agents.install()
    except agentlens.AgentLensConfigurationError as error:
        assert str(error) == "openai-agents is required for agentlens.integrations.openai_agents.install"
    else:
        raise AssertionError("install unexpectedly succeeded without openai-agents")
    assert openai_agents._installation_mode is None
    assert openai_agents._installed_processor is None

assert_install_fails_without_publishing_state()
assert_install_fails_without_publishing_state()
print("fresh-optional-dependency-check-ok")
"""
        completed = subprocess.run(
            [sys.executable, "-S", "-c", code],
            cwd=Path(__file__).resolve().parents[1],
            env=environment,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(
            completed.returncode,
            0,
            completed.stdout + completed.stderr,
        )
        self.assertIn("fresh-optional-dependency-check-ok", completed.stdout)

    def test_install_does_not_enable_agentlens_or_create_transport(self):
        self.assertFalse(openai_agents.sdk._configuration.enabled)
        self.assertIsNone(openai_agents.sdk._transport_sink)

        openai_agents.install()

        self.assertFalse(openai_agents.sdk._configuration.enabled)
        self.assertIsNone(openai_agents.sdk._transport_sink)

    def test_installed_object_is_the_issue_08_processor(self):
        openai_agents.install()

        installed = openai_agents._installed_processor
        self.assertIsInstance(installed, openai_agents.AgentLensOpenAIProcessor)
        self.assertIs(self.fake_agents.processors[0], installed)


if __name__ == "__main__":
    unittest.main()
