# -*- coding: utf-8 -*-

import tempfile
import time
import unittest
from pathlib import Path

from app import main
from app.sqlite_store import SQLiteRunStore
from studio.retry import _generate_with_retry


class WorkerLifecycleTests(unittest.TestCase):
    def test_stop_allows_immediate_restart_while_sdk_call_finishes_late(self):
        class SlowService:
            provider = "OpenRouter"
            model_name = "slow-test-model"

            @staticmethod
            def generate(*args, **kwargs):
                time.sleep(2)
                return "Late result"

        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteRunStore(Path(temp_dir) / "state.sqlite3")
            token = main._current_store.set(store)
            try:
                def first_worker():
                    _generate_with_retry(
                        SlowService(),
                        "system",
                        "user",
                        max_retries=0,
                        store=store,
                        task_label="First task",
                    )

                self.assertTrue(main._start_background(first_worker))
                time.sleep(0.1)
                started_at = time.time()
                main.stop_run()
                self.assertLess(time.time() - started_at, 1.5)
                self.assertFalse(main._thread_alive())
                self.assertTrue(main._start_background(lambda: None))
                store.thread.join(timeout=1)
                self.assertFalse(main._thread_alive())
                self.assertFalse(any("previous background run is still shutting down" in line for line in store.log))
            finally:
                main._current_store.reset(token)


if __name__ == "__main__":
    unittest.main()
