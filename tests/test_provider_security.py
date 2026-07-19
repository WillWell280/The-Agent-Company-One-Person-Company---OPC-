# -*- coding: utf-8 -*-

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.sqlite_store import SQLiteRunStore
from studio.generic_engine import _generate_image_asset
from studio.llm_service import MOCK_PROVIDER, PROVIDERS


class ProviderSecurityTests(unittest.TestCase):
    def test_provider_catalog_contains_only_public_integrations(self):
        self.assertEqual(
            PROVIDERS,
            [
                MOCK_PROVIDER,
                "OpenRouter",
                "Google Gemini",
                "OpenAI (GPT)",
                "Anthropic (Claude)",
            ],
        )

    def test_unsupported_saved_provider_is_reset_to_demo(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "state.sqlite3"
            store = SQLiteRunStore(db_path)
            employee_key = next(iter(store.workflow["employees"]))

            with sqlite3.connect(db_path) as connection:
                payload = json.loads(
                    connection.execute(
                        "SELECT payload FROM app_state WHERE id = 1"
                    ).fetchone()[0]
                )
                payload["global_config"] = {
                    "provider": "Retired Internal Provider",
                    "key": "must-not-survive",
                    "model": "retired-model",
                }
                payload["emp_configs"][employee_key] = {
                    "provider": "Retired Internal Provider",
                    "key": "must-not-survive",
                    "model": "retired-model",
                }
                connection.execute(
                    "UPDATE app_state SET payload = ? WHERE id = 1",
                    (json.dumps(payload),),
                )
                connection.commit()

            reloaded = SQLiteRunStore(db_path)

            self.assertEqual(reloaded.global_config["provider"], MOCK_PROVIDER)
            self.assertEqual(reloaded.global_config["model"], "mock-studio-model")
            self.assertEqual(reloaded.global_config["key"], "")
            self.assertEqual(reloaded.emp_configs[employee_key]["provider"], MOCK_PROVIDER)
            self.assertEqual(reloaded.emp_configs[employee_key]["model"], "mock-studio-model")
            self.assertEqual(reloaded.emp_configs[employee_key]["key"], "")

    def test_persisted_model_key_is_never_restored(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "state.sqlite3"
            store = SQLiteRunStore(db_path)
            employee_key = next(iter(store.workflow["employees"]))

            with sqlite3.connect(db_path) as connection:
                payload = json.loads(
                    connection.execute(
                        "SELECT payload FROM app_state WHERE id = 1"
                    ).fetchone()[0]
                )
                payload["emp_configs"][employee_key] = {
                    "provider": "OpenAI (GPT)",
                    "key": "must-not-survive",
                    "model": "gpt-4o",
                }
                connection.execute(
                    "UPDATE app_state SET payload = ? WHERE id = 1",
                    (json.dumps(payload),),
                )
                connection.commit()

            reloaded = SQLiteRunStore(db_path)

            self.assertEqual(reloaded.emp_configs[employee_key]["provider"], "OpenAI (GPT)")
            self.assertEqual(reloaded.emp_configs[employee_key]["model"], "gpt-4o")
            self.assertEqual(reloaded.emp_configs[employee_key]["key"], "")

    def test_image_fallback_does_not_call_an_unconfigured_service(self):
        store = SimpleNamespace(
            workflow={"name": "Image Test"},
            input_package={"text": "", "assets": []},
            emp_configs={
                "worker": {
                    "provider": MOCK_PROVIDER,
                    "key": "",
                    "model": "mock-studio-model",
                }
            },
        )
        task = {
            "id": 1,
            "title": "Create an Image",
            "owner": "worker",
            "desc": "Create a clean product image.",
            "method": "Use a simple composition.",
            "output_modes": "image",
        }

        with patch("studio.generic_engine.urllib.request.urlopen") as urlopen:
            asset, error = _generate_image_asset(store, task, "A clean product image")

        self.assertIsNone(error)
        self.assertEqual(asset["generated_by"], "local_fallback_renderer")
        urlopen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
