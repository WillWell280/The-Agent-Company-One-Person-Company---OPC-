import unittest
import threading
import time
from unittest.mock import patch

from studio.retry import MODEL_CALL_HEARTBEAT_SECONDS, _generate_with_retry


class FakeStore:
    def __init__(self, cancel=False):
        self.cancel = cancel
        self.log = []

    def log_line(self, message):
        self.log.append(message)


class SequenceService:
    provider = "OpenAI (GPT)"

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def generate(self, system_prompt, user_prompt, **kwargs):
        self.calls.append((system_prompt, user_prompt, kwargs))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class RetryTests(unittest.TestCase):
    def test_default_heartbeat_interval_is_180_seconds(self):
        self.assertEqual(MODEL_CALL_HEARTBEAT_SECONDS, 180)

    def test_success_preserves_prompts_mock_key_and_attachments(self):
        service = SequenceService(["Complete"])
        store = FakeStore()
        attachments = [{"name": "reference.png", "data": "abc"}]

        result = _generate_with_retry(
            service,
            "system",
            "user",
            mock_key="generic_task:1:Test",
            store=store,
            attachments=attachments,
        )

        self.assertEqual(result, "Complete")
        self.assertEqual(len(service.calls), 1)
        self.assertEqual(service.calls[0][0:2], ("system", "user"))
        self.assertEqual(service.calls[0][2]["mock_key"], "generic_task:1:Test")
        self.assertTrue(service.calls[0][2]["raise_on_error"])
        self.assertIs(service.calls[0][2]["attachments"], attachments)

    def test_transient_error_retries_and_recovers(self):
        service = SequenceService([TimeoutError("timed out"), "Recovered"])
        store = FakeStore()

        with patch("studio.retry._wait_for_retry_delay", return_value=True):
            result = _generate_with_retry(
                service,
                "system",
                "user",
                max_retries=1,
                store=store,
                task_label="Test task",
            )

        self.assertEqual(result, "Recovered")
        self.assertEqual(len(service.calls), 2)
        self.assertTrue(any("transient network/API error" in line for line in store.log))
        self.assertTrue(any("network/API access recovered" in line for line in store.log))

    def test_permanent_error_does_not_retry(self):
        service = SequenceService([RuntimeError("invalid api key")])
        store = FakeStore()

        result = _generate_with_retry(
            service,
            "system",
            "user",
            max_retries=3,
            store=store,
        )

        self.assertTrue(result.startswith("❌ API request failed after 0 retries"))
        self.assertEqual(len(service.calls), 1)
        self.assertTrue(any("non-transient error" in line for line in store.log))

    def test_cancel_stops_before_model_call(self):
        service = SequenceService(["Should not be called"])

        result = _generate_with_retry(
            service,
            "system",
            "user",
            store=FakeStore(cancel=True),
            task_label="Cancellation test",
        )

        self.assertEqual(result, "❌ Canceled: Cancellation test received a stop request before the API call.")
        self.assertEqual(service.calls, [])

    def test_cancel_stops_waiting_for_in_flight_model_call(self):
        store = FakeStore()

        class SlowService:
            provider = "OpenRouter"
            model_name = "slow-test-model"

            @staticmethod
            def generate(*args, **kwargs):
                time.sleep(2)
                return "Late result"

        timer = threading.Timer(0.1, lambda: setattr(store, "cancel", True))
        timer.start()
        started_at = time.time()
        try:
            result = _generate_with_retry(
                SlowService(),
                "system",
                "user",
                max_retries=0,
                store=store,
                task_label="In-flight cancellation test",
            )
        finally:
            timer.cancel()

        self.assertLess(time.time() - started_at, 1)
        self.assertEqual(result, "❌ Canceled: In-flight cancellation test stopped waiting for the model response.")
        self.assertTrue(any("late API response" in line and "will be ignored" in line for line in store.log))

    def test_long_model_call_reports_heartbeat(self):
        store = FakeStore()

        class SlowService:
            provider = "OpenRouter"
            model_name = "slow-test-model"

            @staticmethod
            def generate(*args, **kwargs):
                time.sleep(0.12)
                return "Complete"

        with (
            patch("studio.retry.MODEL_CALL_HEARTBEAT_SECONDS", 0.04),
            patch("studio.retry.MODEL_CALL_CANCEL_POLL_SECONDS", 0.01),
        ):
            result = _generate_with_retry(
                SlowService(),
                "system",
                "user",
                max_retries=0,
                store=store,
                task_label="Long-running task",
            )

        self.assertEqual(result, "Complete")
        self.assertTrue(any("is still processing" in line for line in store.log))


if __name__ == "__main__":
    unittest.main()
