# -*- coding: utf-8 -*-

import unittest
from types import SimpleNamespace

from jinja2 import Environment, FileSystemLoader


class ResultsTemplateTests(unittest.TestCase):
    def test_result_card_uses_current_task_title_not_stale_short_label(self):
        environment = Environment(loader=FileSystemLoader("app/templates"))
        html = environment.get_template("partials/results.html").render(
            tasks=[{
                "id": 1,
                "title": "Current Workflow Task Title",
                "short": "Stale Template Label",
                "owner_emoji": "👤",
                "owner_name": "Test Agent",
                "badge": "🟡 Ready",
                "done": False,
                "ready": True,
                "output_text": "",
                "output_html": "",
                "assets": [],
                "reviews": [],
            }],
            done_count=0,
            results_revision="test",
            store=SimpleNamespace(running_task=None),
        )
        self.assertIn("Current Workflow Task Title", html)
        self.assertNotIn("Stale Template Label", html)


if __name__ == "__main__":
    unittest.main()
