# -*- coding: utf-8 -*-
"""SQLite runtime store for the FastAPI application.

Design goals:
- Support fully configurable agents and tasks.
- Persist workflows, inputs, and outputs so refreshes and disconnects are recoverable.
- Keep API keys in process memory only and remove them from all SQLite payloads.
"""

import json
import os
import sqlite3
import tempfile
import threading
import time
import uuid
from pathlib import Path

from studio.generic_workflow import (
    clone_default_workflow,
    make_output_package,
    normalize_workflow,
    ordered_tasks,
)
from studio.llm_service import MOCK_PROVIDER, PROVIDERS
from studio.web_search import (
    SEARCH_PROVIDER_NONE,
    default_search_config,
    normalize_search_provider,
    search_api_key,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RUNTIME_DIR = (
    os.environ.get("GENERIC_AGENT_RUNTIME_DIR")
    or os.environ.get("SCRIPT_STUDIO_RUNTIME_DIR")
    or os.environ.get("RAILWAY_VOLUME_MOUNT_PATH")
    or str(PROJECT_ROOT / ".generic_agent_runtime")
)
DEFAULT_DB_PATH = os.environ.get(
    "GENERIC_AGENT_DB",
    os.environ.get(
        "SCRIPT_STUDIO_DB",
        str(Path(DEFAULT_RUNTIME_DIR) / "fastapi_run_state.sqlite3"),
    ),
)

LEGACY_DEFAULT_WORKFLOW_NAME = "\u901a\u7528 AI Agent \u534f\u4f5c\u5de5\u4f5c\u53f0"
LEGACY_DEFAULT_EMPLOYEE_NAMES = {
    "manager": "\u5de5\u4f5c\u6d41\u7ba1\u7406\u8005",
    "analyst": "\u9700\u6c42\u5206\u6790\u5e08",
    "planner": "\u65b9\u6848\u8bbe\u8ba1\u5e08",
    "executor": "\u6267\u884c\u4e13\u5bb6",
    "reviewer": "\u8d28\u91cf\u5ba1\u6838\u5458",
}
LEGACY_DEFAULT_TASK_TITLES = (
    "\u7406\u89e3\u7528\u6237\u76ee\u6807\u4e0e\u8f93\u5165\u6750\u6599",
    "\u5236\u5b9a\u89e3\u51b3\u65b9\u6848\u4e0e\u6267\u884c\u8ba1\u5212",
    "\u751f\u6210\u6838\u5fc3\u4ea4\u4ed8\u7269",
    "\u8d28\u91cf\u5ba1\u6838\u4e0e\u6539\u8fdb\u5efa\u8bae",
    "\u5f62\u6210\u6700\u7ec8\u4ea4\u4ed8\u7248\u672c",
)


def _migrate_legacy_default_workflow(workflow):
    """Replace the untouched legacy default workflow with its English version."""
    if not isinstance(workflow, dict) or workflow.get("name") != LEGACY_DEFAULT_WORKFLOW_NAME:
        return workflow, False
    employees = workflow.get("employees")
    tasks = workflow.get("tasks")
    if not isinstance(employees, dict) or not isinstance(tasks, list):
        return workflow, False
    employee_names_match = all(
        isinstance(employees.get(key), dict)
        and employees[key].get("name") == expected_name
        for key, expected_name in LEGACY_DEFAULT_EMPLOYEE_NAMES.items()
    )
    task_titles = tuple(
        task.get("title")
        for task in tasks
        if isinstance(task, dict)
    )
    if employee_names_match and task_titles == LEGACY_DEFAULT_TASK_TITLES:
        return clone_default_workflow(), True
    return workflow, False


class SQLiteRunStore:
    """Thread-safe runtime state store for configurable agent workflows."""

    def __init__(self, db_path=DEFAULT_DB_PATH):
        self.db_path = Path(db_path)
        self.lock = threading.RLock()
        self.thread = None
        self._init_defaults()
        self._init_db()
        self._load_state()

    def _init_defaults(self):
        self.workflow = normalize_workflow(clone_default_workflow())
        self.input_package = make_output_package("")
        self.outputs = {task["id"]: None for task in ordered_tasks(self.workflow)}
        self.memory = {key: [] for key in self.workflow["employees"]}
        self.running_task = None
        self.running_employee = None
        self.is_running = False
        self.cancel = False
        self.failed_task = None
        self.log = []
        self.manager_reviews = {}
        self.global_config = {"provider": MOCK_PROVIDER, "key": "", "model": "mock-studio-model"}
        self.emp_configs = {
            key: {"provider": MOCK_PROVIDER, "key": "", "model": "mock-studio-model"}
            for key in self.workflow["employees"]
        }
        self.search_config = default_search_config()
        self.per_emp = False
        self.doc_history = []
        self.workflow_templates = []
        self.interrupted_task = None
        self.interrupted_at = ""
        self.last_saved_at = ""

    def _init_db(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS app_state (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.commit()

    @staticmethod
    def _normalize_model_config(cfg):
        cfg = cfg if isinstance(cfg, dict) else {}
        provider = cfg.get("provider", MOCK_PROVIDER)
        if provider == "Mock (\u6f14\u793a)":
            provider = MOCK_PROVIDER
        if provider not in PROVIDERS:
            return {
                "provider": MOCK_PROVIDER,
                "key": "",
                "model": "mock-studio-model",
            }
        return {
            "provider": provider,
            "key": str(cfg.get("key") or ""),
            "model": cfg.get("model", "mock-studio-model"),
        }

    @classmethod
    def _sanitize_model_config(cls, cfg):
        clean = cls._normalize_model_config(cfg)
        clean["key"] = ""
        return clean

    @staticmethod
    def _uses_unsupported_provider(cfg):
        cfg = cfg if isinstance(cfg, dict) else {}
        provider = cfg.get("provider", MOCK_PROVIDER)
        if provider == "Mock (\u6f14\u793a)":
            provider = MOCK_PROVIDER
        return provider not in PROVIDERS

    def _sanitize_emp_configs(self):
        clean = {}
        for emp_key in self.workflow.get("employees", {}):
            cfg = (self.emp_configs or {}).get(emp_key) or self.global_config
            clean[emp_key] = self._sanitize_model_config(cfg)
        return clean

    @staticmethod
    def _sanitize_search_config(cfg):
        cfg = cfg if isinstance(cfg, dict) else {}
        return {
            "provider": normalize_search_provider(cfg.get("provider")),
            "key": "",
        }

    def _coerce_task_id(self, value):
        try:
            tid = int(value)
        except (TypeError, ValueError):
            return None
        valid = {task["id"] for task in ordered_tasks(self.workflow)}
        return tid if tid in valid else None

    def _normalize_outputs_for_workflow(self, raw_outputs=None):
        raw_outputs = raw_outputs if isinstance(raw_outputs, dict) else {}
        outputs = {}
        for task in ordered_tasks(self.workflow):
            tid = task["id"]
            value = raw_outputs.get(str(tid), raw_outputs.get(tid))
            outputs[tid] = make_output_package(value) if value else None
        return outputs

    def _normalize_memory_for_workflow(self, raw_memory=None):
        raw_memory = raw_memory if isinstance(raw_memory, dict) else {}
        return {
            key: raw_memory.get(key, []) if isinstance(raw_memory.get(key, []), list) else []
            for key in self.workflow.get("employees", {})
        }

    def _normalize_configs_for_workflow(self, raw_configs=None):
        raw_configs = raw_configs if isinstance(raw_configs, dict) else {}
        return {
            key: self._normalize_model_config(raw_configs.get(key) or self.global_config)
            for key in self.workflow.get("employees", {})
        }

    def _normalize_workflow_templates(self, raw_templates=None):
        raw_templates = raw_templates if isinstance(raw_templates, list) else []
        templates = []
        seen = set()
        for item in raw_templates:
            if not isinstance(item, dict):
                continue
            raw_workflow, migrated = _migrate_legacy_default_workflow(item.get("workflow") or {})
            workflow = normalize_workflow(raw_workflow)
            template_id = str(item.get("id") or uuid.uuid4().hex)
            if template_id in seen:
                template_id = uuid.uuid4().hex
            seen.add(template_id)
            saved_name = item.get("name")
            if migrated and saved_name == LEGACY_DEFAULT_WORKFLOW_NAME:
                saved_name = workflow.get("name")
            name = str(saved_name or workflow.get("name") or "Untitled Workflow Template").strip()
            templates.append({
                "id": template_id,
                "name": name or "Untitled Workflow Template",
                "description": str(item.get("description") or workflow.get("description") or "").strip(),
                "workflow": workflow,
                "updated_at": str(item.get("updated_at") or item.get("time") or ""),
            })
        return templates[-50:]

    def _state_payload_locked(self):
        self.last_saved_at = time.strftime("%Y-%m-%d %H:%M:%S")
        return {
            "version": 2,
            "updated_at": self.last_saved_at,
            "workflow": self.workflow,
            "input_package": self.input_package,
            "outputs": {str(k): v for k, v in self.outputs.items()},
            "memory": self.memory,
            "running_task": self.running_task,
            "running_employee": self.running_employee,
            "is_running": self.is_running,
            "failed_task": self.failed_task,
            "log": self.log,
            "manager_reviews": self.manager_reviews,
            "global_config": self._sanitize_model_config(self.global_config),
            "emp_configs": self._sanitize_emp_configs(),
            "search_config": self._sanitize_search_config(self.search_config),
            "per_emp": self.per_emp,
            "doc_history": self.doc_history,
            "workflow_templates": self.workflow_templates,
            "interrupted_task": self.interrupted_task,
            "interrupted_at": self.interrupted_at,
        }

    def _save_state_locked(self):
        payload = self._state_payload_locked()
        raw = json.dumps(payload, ensure_ascii=False)
        tmp_fd, tmp_path = tempfile.mkstemp(
            prefix="generic_state_", suffix=".json.tmp", dir=str(self.db_path.parent), text=True
        )
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                f.write(raw)
            with open(tmp_path, "r", encoding="utf-8") as f:
                checked_raw = f.read()
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    """
                    INSERT INTO app_state (id, payload, updated_at)
                    VALUES (1, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        payload = excluded.payload,
                        updated_at = excluded.updated_at
                    """,
                    (checked_raw, self.last_saved_at),
                )
                conn.commit()
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    def save_state(self):
        with self.lock:
            self._save_state_locked()

    def _load_state(self):
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute("SELECT payload FROM app_state WHERE id = 1").fetchone()
        if not row:
            self.save_state()
            return
        try:
            data = json.loads(row[0])
        except Exception as exc:
            self.log.append(f"[{time.strftime('%H:%M:%S')}] ⚠️ Could not load runtime state from SQLite: {exc}")
            return

        if data.get("version") != 2:
            self.doc_history = data.get("doc_history", []) if isinstance(data.get("doc_history"), list) else []
            self.log.append(
                f"[{time.strftime('%H:%M:%S')}] ℹ️ Legacy workspace state was detected and migrated "
                "to the default configurable workflow. Previous task outputs were not imported into the new workflow."
            )
            self.save_state()
            return

        raw_workflow, workflow_migrated = _migrate_legacy_default_workflow(
            data.get("workflow") or clone_default_workflow()
        )
        raw_global_config = data.get("global_config") or self.global_config
        raw_emp_configs = data.get("emp_configs")
        raw_emp_configs = raw_emp_configs if isinstance(raw_emp_configs, dict) else {}
        provider_migrated = (
            self._uses_unsupported_provider(raw_global_config)
            or any(self._uses_unsupported_provider(cfg) for cfg in raw_emp_configs.values())
        )
        self.workflow = normalize_workflow(raw_workflow)
        self.input_package = make_output_package(data.get("input_package") or "")
        self.global_config = self._sanitize_model_config(raw_global_config)
        saved_search = self._sanitize_search_config(data.get("search_config"))
        environment_search = default_search_config()
        search_provider = saved_search["provider"]
        if (
            search_provider == SEARCH_PROVIDER_NONE
            and environment_search["provider"] != SEARCH_PROVIDER_NONE
        ):
            search_provider = environment_search["provider"]
        self.search_config = {
            "provider": search_provider,
            "key": search_api_key({"provider": search_provider}),
        }
        self.per_emp = bool(data.get("per_emp", False))
        persisted_emp_configs = {
            key: self._sanitize_model_config(cfg)
            for key, cfg in raw_emp_configs.items()
        }
        self.emp_configs = self._normalize_configs_for_workflow(persisted_emp_configs)
        self.outputs = self._normalize_outputs_for_workflow(data.get("outputs"))
        self.memory = self._normalize_memory_for_workflow(data.get("memory"))
        self.log = data.get("log", []) if isinstance(data.get("log"), list) else []
        self.manager_reviews = data.get("manager_reviews", {}) if isinstance(data.get("manager_reviews"), dict) else {}
        self.doc_history = data.get("doc_history", []) if isinstance(data.get("doc_history"), list) else []
        self.workflow_templates = self._normalize_workflow_templates(data.get("workflow_templates"))
        self.failed_task = self._coerce_task_id(data.get("failed_task"))
        self.last_saved_at = data.get("updated_at", "")
        self.running_task = None
        self.running_employee = None
        self.is_running = False

        if data.get("is_running"):
            self.interrupted_task = (
                self._coerce_task_id(data.get("running_task"))
                or self._coerce_task_id(data.get("interrupted_task"))
            )
            self.interrupted_at = data.get("updated_at", "")
            self.log.append(
                f"[{time.strftime('%H:%M:%S')}] ⚠️ The previous run was interrupted during "
                f"Task {self.interrupted_task or '?'}. Select Resume Run to continue."
            )
            self.save_state()
        else:
            self.interrupted_task = self._coerce_task_id(data.get("interrupted_task"))
            self.interrupted_at = data.get("interrupted_at", "")
            if workflow_migrated or provider_migrated:
                if workflow_migrated:
                    self.log.append(
                        f"[{time.strftime('%H:%M:%S')}] ℹ️ Updated the built-in workflow defaults to the English edition."
                    )
                if provider_migrated:
                    self.log.append(
                        f"[{time.strftime('%H:%M:%S')}] ℹ️ A saved model provider is no longer supported "
                        "and was reset to Demo mode."
                    )
                self.save_state()

    def reset(self):
        with self.lock:
            docs = list(self.doc_history)
            self.cancel = True
            self.outputs = {task["id"]: None for task in ordered_tasks(self.workflow)}
            self.memory = {key: [] for key in self.workflow.get("employees", {})}
            self.running_task = None
            self.running_employee = None
            self.is_running = False
            self.failed_task = None
            self.log = []
            self.manager_reviews = {}
            self.interrupted_task = None
            self.interrupted_at = ""
            self.doc_history = docs
            self._save_state_locked()

    def force_stop(self):
        with self.lock:
            self.cancel = True
            self.is_running = False
            self.running_task = None
            self.running_employee = None
            self.interrupted_task = None
            self.interrupted_at = ""
            self._save_state_locked()

    def log_line(self, msg):
        with self.lock:
            self.log.append(f"[{time.strftime('%H:%M:%S')}] {msg}")
            if len(self.log) > 500:
                self.log = self.log[-500:]
            self._save_state_locked()

    def set_input_package(self, package):
        with self.lock:
            self.input_package = make_output_package(package)
            self._save_state_locked()

    def set_output(self, tid, value, assets=None):
        with self.lock:
            tid = int(tid)
            self.outputs[tid] = make_output_package(value, assets=assets)
            self._save_state_locked()

    def clear_output(self, tid):
        with self.lock:
            tid = int(tid)
            if tid in self.outputs:
                self.outputs[tid] = None
            prefix = f"{tid}:"
            self.manager_reviews = {
                k: v for k, v in self.manager_reviews.items()
                if k != str(tid) and not k.startswith(prefix)
            }
            self._save_state_locked()

    def add_memory(self, emp_key, note):
        if not emp_key:
            return
        with self.lock:
            mem = self.memory.setdefault(emp_key, [])
            if note not in mem:
                mem.append(note)
                self._save_state_locked()

    def add_manager_review(self, key, review):
        with self.lock:
            reviews = self.manager_reviews.setdefault(str(key), [])
            reviews.append(review)
            if len(reviews) > 10:
                self.manager_reviews[str(key)] = reviews[-10:]
            self._save_state_locked()

    def clear_manager_review(self, key):
        with self.lock:
            self.manager_reviews.pop(str(key), None)
            self._save_state_locked()

    def add_doc_history(self, title, content, assets=None):
        with self.lock:
            if not content:
                return
            archived_assets = []
            for asset in assets or []:
                if not isinstance(asset, dict) or not asset.get("data"):
                    continue
                try:
                    size = int(asset.get("size") or 0)
                except (TypeError, ValueError):
                    size = 0
                archived_assets.append({
                    "id": str(asset.get("id") or uuid.uuid4().hex),
                    "source_asset_id": str(asset.get("source_asset_id") or ""),
                    "task_id": asset.get("task_id"),
                    "task_title": str(asset.get("task_title") or ""),
                    "name": str(asset.get("name") or "Attachment"),
                    "mime": str(asset.get("mime") or "application/octet-stream"),
                    "size": size,
                    "data": str(asset.get("data") or ""),
                })
            if (
                self.doc_history
                and self.doc_history[-1].get("content") == content
                and self.doc_history[-1].get("assets", []) == archived_assets
            ):
                return
            self.doc_history.append({
                "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                "title": title or "Workflow Delivery",
                "content": content,
                "assets": archived_assets,
            })
            if len(self.doc_history) > 50:
                self.doc_history = self.doc_history[-50:]
            self._save_state_locked()

    def clear_doc_history(self):
        with self.lock:
            self.doc_history = []
            self._save_state_locked()

    def delete_doc_history(self, index):
        with self.lock:
            try:
                idx = int(index)
            except (TypeError, ValueError):
                return False
            if not (0 <= idx < len(self.doc_history)):
                return False
            self.doc_history.pop(idx)
            self._save_state_locked()
            return True

    def _unique_workflow_template_name_locked(self, name, exclude_id=""):
        base = str(name or "Untitled Workflow Template").strip() or "Untitled Workflow Template"
        exclude_id = str(exclude_id or "").strip()
        used = {
            str(item.get("name") or "").strip()
            for item in self.workflow_templates
            if str(item.get("id") or "").strip() != exclude_id
        }
        if base not in used:
            return base
        idx = 2
        while f"{base}{idx}" in used:
            idx += 1
        return f"{base}{idx}"

    def save_workflow_template(self, name=None, workflow=None, template_id=None):
        with self.lock:
            workflow = normalize_workflow(workflow or self.workflow)
            now = time.strftime("%Y-%m-%d %H:%M:%S")
            template_id = str(template_id or "").strip()
            name = self._unique_workflow_template_name_locked(
                str(name or workflow.get("name") or "Untitled Workflow Template").strip() or "Untitled Workflow Template",
                exclude_id=template_id,
            )
            workflow = dict(workflow)
            workflow["name"] = name
            entry = {
                "id": template_id or uuid.uuid4().hex,
                "name": name,
                "description": workflow.get("description", ""),
                "workflow": workflow,
                "updated_at": now,
            }
            updated = False
            if template_id:
                for idx, existing in enumerate(self.workflow_templates):
                    if existing.get("id") == template_id:
                        entry["id"] = template_id
                        self.workflow_templates[idx] = entry
                        updated = True
                        break
            if not updated:
                if template_id:
                    entry["id"] = uuid.uuid4().hex
                self.workflow_templates.append(entry)
            self.workflow_templates = self._normalize_workflow_templates(self.workflow_templates)
            self._save_state_locked()
            return entry

    def get_workflow_template(self, template_id):
        template_id = str(template_id or "").strip()
        for template in self.workflow_templates:
            if template.get("id") == template_id:
                return template
        return None

    def delete_workflow_template(self, template_id):
        with self.lock:
            template_id = str(template_id or "").strip()
            before = len(self.workflow_templates)
            self.workflow_templates = [item for item in self.workflow_templates if item.get("id") != template_id]
            changed = len(self.workflow_templates) != before
            if changed:
                self._save_state_locked()
            return changed

    def snapshot_config(self, workflow=None, input_package=None, emp_configs=None,
                        per_emp=None, global_config=None, search_config=None):
        with self.lock:
            if workflow is not None:
                old_outputs = self.outputs
                old_memory = self.memory
                old_configs = self.emp_configs
                self.workflow = normalize_workflow(workflow)
                self.outputs = self._normalize_outputs_for_workflow(old_outputs)
                self.memory = self._normalize_memory_for_workflow(old_memory)
                self.emp_configs = self._normalize_configs_for_workflow(old_configs)
            if input_package is not None:
                self.input_package = make_output_package(input_package)
            if global_config is not None:
                self.global_config = self._normalize_model_config(global_config)
            if emp_configs is not None:
                self.emp_configs = self._normalize_configs_for_workflow(emp_configs)
            if search_config is not None:
                self.search_config = {
                    "provider": normalize_search_provider(search_config.get("provider")),
                    "key": str(search_config.get("key") or ""),
                }
            if per_emp is not None:
                self.per_emp = bool(per_emp)
            self._save_state_locked()

    def mark_interrupted(self, task_id=None):
        with self.lock:
            self.interrupted_task = task_id or self.running_task
            self.interrupted_at = time.strftime("%Y-%m-%d %H:%M:%S")
            self.is_running = False
            self.running_task = None
            self.running_employee = None
            self._save_state_locked()

    def clear_interrupted(self):
        with self.lock:
            self.interrupted_task = None
            self.interrupted_at = ""
            self._save_state_locked()
