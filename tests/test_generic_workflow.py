import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.sqlite_store import SQLiteRunStore
from studio.generic_engine import run_pipeline
from studio.generic_workflow import normalize_workflow, workflow_dependency_issues


def make_workflow(tasks):
    return normalize_workflow({
        "name": "Orchestration Test Company",
        "description": "Validates dependency-aware agent workflow orchestration.",
        "manager_key": "manager",
        "employees": {
            "manager": {
                "name": "Manager",
                "intro": "Reviews task outputs.",
                "skills": "Quality review",
            },
            "worker": {
                "name": "Execution Agent",
                "intro": "Executes assigned tasks.",
                "skills": "Execution",
            },
        },
        "tasks": tasks,
    })


class WorkflowDependencyTests(unittest.TestCase):
    def test_forward_dependency_is_valid(self):
        workflow = make_workflow([
            {"id": 1, "title": "Final Consolidation", "owner": "worker", "deps": [2]},
            {"id": 2, "title": "Upstream Output", "owner": "worker", "deps": []},
        ])

        self.assertEqual(workflow_dependency_issues(workflow), [])

    def test_cycle_is_reported_with_blocked_tasks(self):
        workflow = make_workflow([
            {"id": 1, "title": "Task One", "owner": "worker", "deps": [2]},
            {"id": 2, "title": "Task Two", "owner": "worker", "deps": [1]},
        ])

        issues = workflow_dependency_issues(workflow)

        self.assertEqual(len(issues), 1)
        self.assertIn("Circular dependency", issues[0])
        self.assertIn("Task 1", issues[0])
        self.assertIn("Task 2", issues[0])

    def test_pipeline_runs_forward_dependencies_in_ready_order(self):
        workflow = make_workflow([
            {"id": 1, "title": "Final Consolidation", "owner": "worker", "deps": [2]},
            {"id": 2, "title": "Upstream Output", "owner": "worker", "deps": []},
        ])
        run_order = []

        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteRunStore(Path(temp_dir) / "state.sqlite3")
            store.snapshot_config(workflow=workflow)

            def fake_run_task(bound_store, task_id):
                run_order.append(task_id)
                package = {"text": f"Task {task_id} is complete and satisfies the test requirements.", "assets": []}
                bound_store.set_output(task_id, package)
                return package

            with patch("studio.generic_engine.run_task", side_effect=fake_run_task):
                run_pipeline(store)

            self.assertEqual(run_order, [2, 1])
            self.assertTrue(store.outputs[1])
            self.assertTrue(store.outputs[2])
            self.assertEqual(len(store.doc_history), 1)


if __name__ == "__main__":
    unittest.main()
