# -*- coding: utf-8 -*-

import threading
import time
import unittest
from unittest.mock import patch

from studio.generic_engine import _post_json


class FakeStore:
    def __init__(self):
        self.cancel = False
        self.log = []

    def log_line(self, message):
        self.log.append(message)


class ImageCancellationTests(unittest.TestCase):
    def test_stop_interrupts_wait_for_blocking_image_api(self):
        store = FakeStore()

        def slow_post(*args, **kwargs):
            time.sleep(2)
            return {"data": []}, None, False

        timer = threading.Timer(0.1, lambda: setattr(store, "cancel", True))
        timer.start()
        started_at = time.time()
        try:
            with patch("studio.generic_engine._post_json_once", side_effect=slow_post):
                response, error = _post_json(
                    "https://example.invalid/images",
                    {"prompt": "test"},
                    attempts=1,
                    retry_label="Image cancellation test",
                    store=store,
                )
        finally:
            timer.cancel()

        self.assertLess(time.time() - started_at, 1)
        self.assertIsNone(response)
        self.assertEqual(error, "Canceled while waiting for the image API response.")
        self.assertTrue(any("late API response will be ignored" in line for line in store.log))


if __name__ == "__main__":
    unittest.main()
