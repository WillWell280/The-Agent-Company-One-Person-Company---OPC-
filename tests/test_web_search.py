# -*- coding: utf-8 -*-

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import main
from app.sqlite_store import SQLiteRunStore
from studio import web_search
from studio.generic_engine import _build_task_prompt, _hard_validate, run_task
from studio.generic_workflow import normalize_workflow, package_text


def result(index, prefix="source"):
    return {
        "title": f"{prefix} {index}",
        "url": f"https://example.com/{prefix}/{index}",
        "published_at": f"2026-07-{index:02d}",
        "snippet": f"{prefix} summary {index}",
    }


class WebSearchAdapterTests(unittest.TestCase):
    def test_builds_exactly_three_queries(self):
        queries = web_search.build_search_queries(
            {"name": "Research Company"},
            {"title": "Market Research", "desc": "Analyze market size, competitors, and trends"},
            "Focus on the North American market and use current data",
        )

        self.assertEqual(len(queries), 3)
        self.assertEqual(len(set(queries)), 3)
        self.assertTrue(all("Market Research" in query for query in queries))

    def test_tavily_request_and_response_mapping(self):
        payloads = []

        def fake_request(url, headers=None, payload=None, params=None):
            payloads.append((url, headers, payload, params))
            return {
                "results": [{
                    "title": "Tavily result",
                    "url": "https://example.com/tavily",
                    "published_date": "2026-07-01",
                    "content": "Tavily summary",
                }],
            }

        with patch("studio.web_search._request_json", side_effect=fake_request):
            results = web_search.perform_web_search(
                "Tavily",
                "tvly-key",
                ["query one", "query two", "query three"],
            )

        self.assertEqual(len(payloads), 3)
        self.assertEqual(payloads[0][0], "https://api.tavily.com/search")
        self.assertEqual(payloads[0][1]["Authorization"], "Bearer tvly-key")
        self.assertEqual(payloads[0][2]["max_results"], 5)
        self.assertEqual(results[0]["title"], "Tavily result")

    def test_brave_request_and_response_mapping(self):
        def fake_request(url, headers=None, payload=None, params=None):
            self.assertEqual(
                url,
                "https://api.search.brave.com/res/v1/web/search",
            )
            self.assertEqual(headers["X-Subscription-Token"], "brave-key")
            self.assertEqual(params["count"], 5)
            return {
                "web": {
                    "results": [{
                        "title": "Brave result",
                        "url": "https://example.com/brave",
                        "page_age": "2026-07-02",
                        "description": "Brave summary",
                    }],
                },
            }

        with patch("studio.web_search._request_json", side_effect=fake_request):
            results = web_search.perform_web_search(
                "Brave Search",
                "brave-key",
                ["query one", "query two", "query three"],
            )

        self.assertEqual(results[0]["snippet"], "Brave summary")

    def test_serper_request_and_response_mapping(self):
        def fake_request(url, headers=None, payload=None, params=None):
            self.assertEqual(url, "https://google.serper.dev/search")
            self.assertEqual(headers["X-API-KEY"], "serper-key")
            self.assertEqual(payload["num"], 5)
            return {
                "organic": [{
                    "title": "Serper result",
                    "link": "https://example.com/serper",
                    "date": "2026-07-03",
                    "snippet": "Serper summary",
                }],
            }

        with patch("studio.web_search._request_json", side_effect=fake_request):
            results = web_search.perform_web_search(
                "Serper",
                "serper-key",
                ["query one", "query two", "query three"],
            )

        self.assertEqual(results[0]["published_at"], "2026-07-03")

    def test_anysearch_uses_one_batch_request_and_parses_results(self):
        calls = []

        def fake_request(url, headers=None, payload=None, params=None):
            calls.append((url, headers, payload))
            return {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "content": [{
                        "type": "text",
                        "text": (
                            "## Query 1: query one\n\n"
                            "## Search Results (1 results, 10ms)\n\n"
                            "### 1. AnySearch result\n"
                            "- **URL**: https://example.com/anysearch\n"
                            "- AnySearch summary\n\n"
                            "---\n\n"
                            "## Query 2: query two\n\n"
                            "## Search Results (1 results, 10ms)\n\n"
                            "### 1. Second result\n"
                            "- **URL**: https://example.com/second\n"
                            "- Second summary\n\n"
                            "---\n\n"
                            "## Query 3: query three\n\n"
                            "## Search Results (1 results, 10ms)\n\n"
                            "### 1. Third result\n"
                            "- **URL**: https://example.com/third\n"
                            "- Third summary"
                        ),
                    }],
                },
            }

        with patch("studio.web_search._request_json", side_effect=fake_request):
            results = web_search.perform_web_search(
                "AnySearch",
                "anysearch-key",
                ["query one", "query two", "query three"],
            )

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "https://api.anysearch.com/mcp")
        self.assertEqual(calls[0][1]["Authorization"], "Bearer anysearch-key")
        self.assertEqual(
            calls[0][2]["params"]["name"],
            "batch_search",
        )
        self.assertEqual(len(results), 3)

    def test_bing_search_reports_official_retirement(self):
        with self.assertRaises(web_search.SearchConfigurationError) as raised:
            web_search.perform_web_search(
                "Bing Search",
                "legacy-key",
                ["query one", "query two", "query three"],
            )

        self.assertIn("August 11, 2025", str(raised.exception))

    def test_missing_key_is_a_configuration_error(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(web_search.SearchConfigurationError) as raised:
                web_search.perform_web_search(
                    "Tavily",
                    "",
                    ["query one", "query two", "query three"],
                )

        self.assertIn("API key", str(raised.exception))

    def test_search_context_has_required_shape_and_source_list(self):
        results = [result(index) for index in range(1, 6)]
        context = web_search.format_search_context(
            "Tavily",
            ["query one", "query two", "query three"],
            results,
            retrieved_at="2026-07-19 12:00:00",
        )
        output = web_search.append_source_list(
            "This conclusion is based on external research. [Source 1]",
            results,
        )

        self.assertIn("[WEB RESEARCH · RETRIEVED: 2026-07-19 12:00:00]", context)
        self.assertIn("[Source 5] source 5", context)
        self.assertIn("URL: https://example.com/source/5", context)
        self.assertIn("Published: 2026-07-05", context)
        self.assertIn("Snippet: source summary 5", context)
        self.assertIn("## Sources (System Generated)", output)
        self.assertTrue(web_search.output_has_inline_source_citation(output))


class WebSearchWorkflowTests(unittest.TestCase):
    def _workflow(self, web_search_enabled=True):
        return normalize_workflow({
            "name": "Web Research Company",
            "manager_key": "manager",
            "employees": {
                "manager": {"name": "Manager"},
                "worker": {"name": "Researcher"},
            },
            "tasks": [{
                "id": 1,
                "title": "Web Research",
                "owner": "worker",
                "deps": [],
                "desc": "Find and summarize current information",
                "method": "Cross-check multiple authoritative sources",
                "acceptance": "The body must include source citations",
                "output_modes": "text",
                "web_search": web_search_enabled,
            }],
        })

    def test_search_key_is_not_written_to_sqlite(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "state.sqlite3"
            store = SQLiteRunStore(db_path)
            store.snapshot_config(
                workflow=self._workflow(),
                search_config={
                    "provider": "Tavily",
                    "key": "secret-search-key",
                },
            )
            with sqlite3.connect(db_path) as connection:
                payload = connection.execute(
                    "SELECT payload FROM app_state WHERE id = 1"
                ).fetchone()[0]
            saved = json.loads(payload)

            self.assertNotIn("secret-search-key", payload)
            self.assertEqual(saved["search_config"]["provider"], "Tavily")
            self.assertEqual(saved["search_config"]["key"], "")
            self.assertTrue(store.search_config["key"])

    def test_prompt_includes_search_context(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteRunStore(Path(temp_dir) / "state.sqlite3")
            workflow = self._workflow()
            store.snapshot_config(workflow=workflow)
            task = workflow["tasks"][0]

            prompt = _build_task_prompt(
                store,
                task,
                search_context="[WEB RESEARCH · RETRIEVED: TEST]\n[Source 1] Title",
            )

        self.assertIn("[WEB RESEARCH · RETRIEVED: TEST]", prompt)
        self.assertIn("must be followed immediately by its [Source N] citation", web_search.format_search_context(
            "Tavily",
            ["q1", "q2", "q3"],
            [result(1)],
        ))

    def test_run_task_searches_once_before_manager_rework(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteRunStore(Path(temp_dir) / "state.sqlite3")
            workflow = self._workflow()
            store.snapshot_config(
                workflow=workflow,
                search_config={"provider": "Tavily", "key": "test-key"},
            )
            search_calls = []
            generation_calls = []
            review_rounds = [
                {
                    "passed": False,
                    "fatal": False,
                    "summary": "Needs revision",
                    "suggestions": "Add the conclusion",
                },
                {
                    "passed": True,
                    "fatal": False,
                    "summary": "Approved",
                    "suggestions": "",
                },
            ]

            def fake_search(provider, key, queries, max_results):
                search_calls.append((provider, key, tuple(queries), max_results))
                return [result(index) for index in range(1, 6)]

            def fake_generate(*args, **kwargs):
                generation_calls.append(args[2])
                return "Web research result cross-checked against multiple sources. [Source 1]"

            with (
                patch("studio.generic_engine.perform_web_search", side_effect=fake_search),
                patch("studio.generic_engine._generate_with_retry", side_effect=fake_generate),
                patch("studio.generic_engine._validate_by_manager", side_effect=review_rounds),
            ):
                package = run_task(store, 1, max_retries=0)

        self.assertEqual(len(search_calls), 1)
        self.assertEqual(len(generation_calls), 2)
        self.assertTrue(all("[WEB RESEARCH · RETRIEVED:" in prompt for prompt in generation_calls))
        self.assertIn("## Sources (System Generated)", package_text(package))
        self.assertEqual(package_text(package).count("## Sources (System Generated)"), 1)

    def test_run_task_without_search_configuration_returns_error(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteRunStore(Path(temp_dir) / "state.sqlite3")
            store.snapshot_config(workflow=self._workflow())

            output = run_task(store, 1, max_retries=0)
            store.failed_task = 1
            token = main._current_store.set(store)
            try:
                runtime = main._runtime_payload()
            finally:
                main._current_store.reset(token)

        self.assertTrue(output.startswith("❌ Web research failed:"))
        self.assertIn("search provider", output)
        self.assertTrue(package_text(store.outputs[1]).startswith("❌ Web research failed:"))
        self.assertTrue(runtime["search_failure_hint"])

    def test_hard_validation_rejects_missing_inline_citation(self):
        package = {
            "text": (
                "This web research result is long enough, but the body has no inline source citation."
                "\n\n## Sources (System Generated)\n- [Source 1] Example"
            ),
            "assets": [],
        }

        passed, issues = _hard_validate(self._workflow()["tasks"][0], package)

        self.assertFalse(passed)
        self.assertIn("[Source N]", issues)


if __name__ == "__main__":
    unittest.main()
