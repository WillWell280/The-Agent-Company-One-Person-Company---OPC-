# -*- coding: utf-8 -*-

import base64
import tempfile
import unittest
from pathlib import Path

from app import main
from app.sqlite_store import SQLiteRunStore
from studio.generic_engine import compile_delivery_assets, compile_delivery_doc
from studio.generic_workflow import normalize_workflow


class DeliveryArchiveTests(unittest.TestCase):
    def test_archive_collects_all_task_text_and_deduplicates_handoff_assets(self):
        workflow = normalize_workflow({
            "name": "Archive Test Company",
            "manager_key": "manager",
            "employees": {
                "manager": {"name": "Manager"},
                "worker": {"name": "Execution Agent"},
            },
            "tasks": [
                {"id": 1, "title": "Text Task", "owner": "worker", "deps": []},
                {"id": 2, "title": "Summary Task", "owner": "worker", "deps": [1]},
            ],
        })
        image_data = base64.b64encode(b"image-content").decode("ascii")
        file_data = base64.b64encode(b"file-content").decode("ascii")

        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteRunStore(Path(temp_dir) / "state.sqlite3")
            store.snapshot_config(workflow=workflow)
            store.set_output(1, {
                "text": "Text output from the first task",
                "assets": [{
                    "id": "image-a",
                    "name": "concept.png",
                    "mime": "image/png",
                    "size": 13,
                    "data": image_data,
                }],
            })
            store.set_output(2, {
                "text": "Text output from the second task",
                "assets": [
                    {
                        "id": "handoff-image",
                        "source_asset_id": "image-a",
                        "source_task_id": 1,
                        "source_task_title": "Text Task",
                        "name": "concept.png",
                        "mime": "image/png",
                        "size": 13,
                        "data": image_data,
                    },
                    {
                        "id": "file-b",
                        "name": "deliverable.pdf",
                        "mime": "application/pdf",
                        "size": 12,
                        "data": file_data,
                    },
                ],
            })

            content = compile_delivery_doc(store)
            assets = compile_delivery_assets(store)

            self.assertIn("## Task 1 · Text Task", content)
            self.assertIn("Text output from the first task", content)
            self.assertIn("## Task 2 · Summary Task", content)
            self.assertIn("Text output from the second task", content)
            self.assertNotIn("Final Asset", content)
            self.assertEqual([asset["name"] for asset in assets], ["concept.png", "deliverable.pdf"])
            self.assertEqual([asset["task_id"] for asset in assets], [1, 2])

            store.add_doc_history("Archive Test Company", content, assets=assets)
            store.add_doc_history("Archive Test Company", content, assets=assets)
            self.assertEqual(len(store.doc_history), 1)
            reloaded = SQLiteRunStore(Path(temp_dir) / "state.sqlite3")
            self.assertEqual(reloaded.doc_history[0]["content"], content)
            self.assertEqual(len(reloaded.doc_history[0]["assets"]), 2)

            token = main._current_store.set(reloaded)
            try:
                response = main.download_doc_asset(0, assets[0]["id"])
                self.assertEqual(response.body, b"image-content")
                self.assertEqual(response.media_type, "image/png")
            finally:
                main._current_store.reset(token)


if __name__ == "__main__":
    unittest.main()
