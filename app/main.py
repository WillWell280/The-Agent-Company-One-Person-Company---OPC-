# -*- coding: utf-8 -*-
"""FastAPI web entry point for the AI Agent Collaboration Workspace."""

import base64
import contextvars
import hashlib
import html
import json
import re
import threading
import uuid
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from studio.generic_engine import (
    TASK_MAX_RETRIES,
    compile_delivery_doc,
    run_pipeline,
    run_task,
    validate_manual_output,
)
from studio.file_extractor import extract_file_content
from studio.generic_workflow import (
    CONTEXT_SCOPE_OPTIONS,
    clone_default_workflow,
    context_scope_label,
    downstream_task_ids,
    infer_context_scope,
    is_ready,
    make_employee_key,
    make_output_package,
    next_task_id,
    normalize_workflow,
    ordered_tasks,
    package_assets,
    package_done,
    package_text,
    parse_deps,
    parse_output_modes,
    task_done,
    task_map,
    workflow_dependency_issues,
)
from studio.llm_service import MOCK_PROVIDER, MODELS, PROVIDER_LABELS, PROVIDERS
from studio.web_search import (
    BING_RETIREMENT_MESSAGE,
    SEARCH_PROVIDER_NONE,
    SEARCH_PROVIDERS,
    normalize_search_provider,
    search_api_key,
)

from .sqlite_store import DEFAULT_RUNTIME_DIR, SQLiteRunStore


PROJECT_ROOT = Path(__file__).resolve().parents[1]
APP_DIR = Path(__file__).resolve().parent
MAX_ASSET_BYTES = 8 * 1024 * 1024
MAX_ASSETS_PER_PACKAGE = 12

app = FastAPI(title="AI Agent Collaboration Workspace")
app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))
DEFAULT_STORE = SQLiteRunStore()
_current_store = contextvars.ContextVar("generic_agent_store", default=DEFAULT_STORE)
_store_registry = {}
_store_registry_lock = threading.RLock()
SESSION_COOKIE = "generic_agent_session"


class StoreProxy:
    def __getattr__(self, name):
        return getattr(_current_store.get(), name)

    def __setattr__(self, name, value):
        setattr(_current_store.get(), name, value)


store = StoreProxy()


def _valid_session_id(value):
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{32}", value) is not None


def _store_for_session(session_id):
    with _store_registry_lock:
        existing = _store_registry.get(session_id)
        if existing is not None:
            return existing
        session_dir = Path(DEFAULT_RUNTIME_DIR) / "sessions"
        session_dir.mkdir(parents=True, exist_ok=True)
        session_store = SQLiteRunStore(db_path=session_dir / f"{session_id}.sqlite3")
        _store_registry[session_id] = session_store
        return session_store


@app.middleware("http")
async def bind_session_store(request: Request, call_next):
    session_id = request.cookies.get(SESSION_COOKIE)
    new_session = False
    if not _valid_session_id(session_id):
        session_id = uuid.uuid4().hex
        new_session = True
    session_store = _store_for_session(session_id)
    token = _current_store.set(session_store)
    try:
        response = await call_next(request)
    finally:
        _current_store.reset(token)
    if new_session:
        response.set_cookie(
            SESSION_COOKIE,
            session_id,
            httponly=True,
            samesite="lax",
            max_age=60 * 60 * 24 * 365,
        )
    return response


PIPELINE_PALETTE = {
    "done": ("#16a34a", "#eaf7ef"),
    "run": ("#5147ff", "#eeecff"),
    "ready": ("#d8a500", "#fdf6e3"),
    "idle": ("#b8b3a6", "#f6f4ee"),
}
API_FAILURE_HINT_KEYWORDS = (
    "api", "api key", "key", "endpoint", "model", "provider",
    "unauthorized", "invalid_api_key", "rate limit", "quota",
    "401", "403", "429",
)
OUTPUT_MODE_OPTIONS = [
    ("text", "Text"),
    ("image", "Image"),
    ("file", "File"),
]
OUTPUT_MODE_VALUES = tuple(value for value, _ in OUTPUT_MODE_OPTIONS)
EMP_POSITION_PRESETS = [
    (51, 32),
    (39, 55),
    (62, 55),
    (11, 84),
    (25, 85),
    (41, 84),
    (57, 84),
    (73, 84),
    (88, 84),
    (13, 56),
    (26, 56),
    (75, 56),
    (88, 56),
    (51, 64),
]
EMP_COLOR_PALETTE = [
    ("#1f4ed8", "#171717"),
    ("#2bb3c0", "#3a2a22"),
    ("#f2b600", "#5a3a1a"),
    ("#7c5cff", "#241f2e"),
    ("#e5654a", "#2a1a14"),
    ("#16a34a", "#3a2a16"),
    ("#db2777", "#2f1723"),
    ("#0891b2", "#12333c"),
    ("#9333ea", "#21102d"),
    ("#ea580c", "#3a1b0c"),
]


def _sprite_svg(shirt, hair):
    return (
        '<svg class="emp-sprite" viewBox="0 0 24 30" shape-rendering="crispEdges" '
        'xmlns="http://www.w3.org/2000/svg">'
        f'<rect x="6" y="2" width="12" height="6" fill="{hair}"/>'
        f'<rect x="5" y="4" width="2" height="7" fill="{hair}"/>'
        f'<rect x="17" y="4" width="2" height="7" fill="{hair}"/>'
        '<rect x="7" y="6" width="10" height="8" fill="#f5c9a6"/>'
        f'<rect x="7" y="6" width="10" height="2" fill="{hair}"/>'
        '<rect x="9" y="9" width="2" height="2" fill="#26303a"/>'
        '<rect x="13" y="9" width="2" height="2" fill="#26303a"/>'
        '<rect x="10" y="12" width="4" height="1" fill="#d99a78"/>'
        f'<rect x="6" y="13" width="12" height="2" fill="{shirt}"/>'
        f'<rect x="5" y="14" width="14" height="10" fill="{shirt}"/>'
        f'<rect x="3" y="15" width="2" height="7" fill="{shirt}"/>'
        f'<rect x="19" y="15" width="2" height="7" fill="{shirt}"/>'
        '<rect x="3" y="22" width="2" height="2" fill="#f5c9a6"/>'
        '<rect x="19" y="22" width="2" height="2" fill="#f5c9a6"/>'
        '<rect x="10" y="14" width="4" height="3" fill="#ffffff" opacity="0.85"/>'
        "</svg>"
    )


def _employee_position(index):
    if index < len(EMP_POSITION_PRESETS):
        return EMP_POSITION_PRESETS[index]
    extra = index - len(EMP_POSITION_PRESETS)
    cols = 8
    row = extra // cols
    col = extra % cols
    return (10 + col * 11, 34 + (row % 4) * 16)


def _employee_short_name(employee):
    name = str(employee.get("name") or "Agent").strip()
    return name


def _redirect(tab="studio", mode=None, workflow_template_id=None):
    suffix = f"&mode={mode}" if mode else ""
    if workflow_template_id:
        suffix += f"&workflow_template_id={quote(str(workflow_template_id))}"
    return RedirectResponse(f"/?tab={tab}{suffix}", status_code=303)


def _valid_provider(value, fallback=MOCK_PROVIDER):
    if value == "Mock (\u6f14\u793a)":
        value = MOCK_PROVIDER
    if fallback == "Mock (\u6f14\u793a)":
        fallback = MOCK_PROVIDER
    if value in PROVIDERS:
        return value
    if fallback in PROVIDERS:
        return fallback
    return PROVIDERS[0]


def _model_known_for_other_provider(provider, model):
    provider = _valid_provider(provider)
    model = str(model or "").strip()
    if not model or model in (MODELS.get(provider) or []):
        return False
    return any(other != provider and model in (options or []) for other, options in MODELS.items())


def _model_from_form(provider, selected, custom, rendered_provider=None, fallback_model=""):
    provider = _valid_provider(provider)
    rendered_provider = str(rendered_provider or provider)
    if rendered_provider != provider:
        return ""
    selected = str(selected or "").strip()
    custom = str(custom or "").strip()
    if selected and selected not in (MODELS.get(provider) or []):
        selected = ""
    if custom and _model_known_for_other_provider(provider, custom):
        custom = ""
    return custom or selected or fallback_model


def _config_for_render(cfg):
    cfg = dict(cfg or {})
    provider = _valid_provider(cfg.get("provider"), MOCK_PROVIDER)
    model = str(cfg.get("model") or "").strip()
    if _model_known_for_other_provider(provider, model):
        model = ""
    return {
        "provider": provider,
        "provider_label": PROVIDER_LABELS.get(provider, provider),
        "key": cfg.get("key", ""),
        "model": model,
    }


def _global_config_for_render():
    cfg = getattr(store, "global_config", None)
    if isinstance(cfg, dict):
        return _config_for_render(cfg)
    return _config_for_render({"provider": MOCK_PROVIDER, "model": "mock-studio-model"})


def _search_config_for_render():
    cfg = getattr(store, "search_config", None)
    cfg = dict(cfg) if isinstance(cfg, dict) else {}
    provider = normalize_search_provider(cfg.get("provider"))
    return {
        "provider": provider,
        "key": search_api_key(cfg),
        "configured": (
            provider not in {SEARCH_PROVIDER_NONE, "Bing Search"}
            and bool(search_api_key(cfg))
        ),
    }


def _safe_download_name(filename, fallback):
    name = re.sub(r"[\r\n\\/]+", "_", str(filename or "").strip())
    name = re.sub(r"_+", "_", name).strip(" ._")
    return name or fallback


def _ascii_download_name(filename, fallback):
    name = _safe_download_name(filename, fallback)
    ascii_name = name.encode("ascii", "ignore").decode("ascii")
    ascii_name = re.sub(r'[^A-Za-z0-9._-]+', "_", ascii_name).strip("._")
    if "." in fallback and "." not in ascii_name:
        return fallback
    return ascii_name or fallback


def _download_headers(filename, fallback):
    safe_name = _safe_download_name(filename, fallback)
    ascii_name = _ascii_download_name(safe_name, fallback)
    quoted_name = quote(safe_name, safe="")
    return {
        "Content-Disposition": (
            f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{quoted_name}'
        )
    }


def _thread_alive():
    return store.thread is not None and store.thread.is_alive()


def _run_guard(bound_store, target, *args, **kwargs):
    token = _current_store.set(bound_store)
    try:
        target(*args, **kwargs)
    except Exception as exc:
        store.log_line(f"❌ Background execution error: {exc}")
        with store.lock:
            store.failed_task = store.running_task
    finally:
        with store.lock:
            store.running_task = None
            store.running_employee = None
            store.is_running = False
        store.save_state()
        _current_store.reset(token)


def _start_background(target, *args, **kwargs):
    bound_store = _current_store.get()
    if _thread_alive() and bound_store.cancel:
        bound_store.thread.join(timeout=1.0)
    if _thread_alive():
        store.log_line("⚠️ The previous background run is still shutting down. Try again in a moment.")
        return False
    with store.lock:
        store.cancel = False
        store.is_running = True
        store.failed_task = None
        store.interrupted_task = None
        store.interrupted_at = ""
    store.save_state()
    t = threading.Thread(target=_run_guard, args=(bound_store, target, *args), kwargs=kwargs, daemon=True)
    store.thread = t
    t.start()
    return True


def _workflow_ready_to_run():
    issues = workflow_dependency_issues(store.workflow)
    if not issues:
        return True
    for issue in issues:
        store.log_line(f"⚠️ The workflow cannot start: {issue}")
    return False


def _invalidate_downstream(tid):
    for task_id in downstream_task_ids(store.workflow, tid):
        store.clear_output(task_id)


def _run_single_task(tid):
    with store.lock:
        store.running_task = tid
    _invalidate_downstream(tid)
    res = run_task(store, tid)
    if isinstance(res, str) and res.startswith("❌"):
        with store.lock:
            store.failed_task = tid
        store.save_state()


def _run_manual_task_review(tid, package):
    with store.lock:
        store.running_task = tid
    validate_manual_output(store, tid, package)


def _status_badge(tid):
    if store.running_task == tid:
        return "🟣 Running"
    reviews = (store.manager_reviews or {}).get(str(tid), [])
    if reviews and isinstance(reviews[-1], dict) and reviews[-1].get("passed") is False:
        return "🔴 Needs Revision"
    if task_done(store, tid):
        return "🟢 Complete"
    if is_ready(store, tid):
        return "🟡 Ready"
    return "⚪ Waiting on Dependencies"


def _markdown_html(content):
    text = content or ""
    lines = html.escape(text).splitlines()
    out = []
    in_ul = False
    in_code = False
    code_lines = []

    def inline_markup(value):
        value = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", value)
        value = re.sub(r"`([^`]+)`", r"<code>\1</code>", value)
        return value

    def close_ul():
        nonlocal in_ul
        if in_ul:
            out.append("</ul>")
            in_ul = False

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("```"):
            if in_code:
                out.append("<pre>" + "\n".join(code_lines) + "</pre>")
                code_lines = []
                in_code = False
            else:
                close_ul()
                in_code = True
            continue
        if in_code:
            code_lines.append(line)
            continue
        if not stripped:
            close_ul()
            out.append("")
            continue
        if stripped.startswith("#"):
            close_ul()
            level = min(len(stripped) - len(stripped.lstrip("#")), 6)
            body = stripped[level:].strip()
            out.append(f"<h{level}>{inline_markup(body)}</h{level}>")
            continue
        if re.match(r"^[-*]\s+", stripped):
            if not in_ul:
                out.append("<ul>")
                in_ul = True
            item_text = re.sub(r"^[-*]\s+", "", stripped)
            out.append(f"<li>{inline_markup(item_text)}</li>")
            continue
        close_ul()
        out.append(f"<p>{inline_markup(stripped)}</p>")
    close_ul()
    if in_code:
        out.append("<pre>" + "\n".join(code_lines) + "</pre>")
    return "\n".join(out)


def _employee_state(emp_key):
    if store.running_employee == emp_key:
        return "working"
    if store.running_task:
        task = task_map(store.workflow).get(store.running_task)
        if task and task.get("owner") == emp_key:
            return "working"
    if emp_key == store.workflow.get("manager_key"):
        return "done" if any((store.manager_reviews or {}).values()) else "idle"
    owned = [task["id"] for task in ordered_tasks(store.workflow) if task.get("owner") == emp_key]
    if owned and all(task_done(store, tid) for tid in owned):
        return "done"
    return "idle"


def _employee_rows():
    rows = []
    state_labels = {"working": "Working", "done": "Complete", "idle": "Available"}
    for index, (key, employee) in enumerate((store.workflow.get("employees", {}) or {}).items()):
        owned = [task["id"] for task in ordered_tasks(store.workflow) if task.get("owner") == key]
        state = _employee_state(key)
        shirt, hair = EMP_COLOR_PALETTE[index % len(EMP_COLOR_PALETTE)]
        if key == store.workflow.get("manager_key"):
            pin_title = (
                f"{employee.get('name', key)} · {state_labels[state]} · Workflow Manager / Reviewer · "
                f"{sum(len(v) for v in (store.manager_reviews or {}).values())} review rounds"
            )
        else:
            pin_title = (
                f"{employee.get('name', key)} · {state_labels[state]} · "
                f"Tasks {sum(1 for tid in owned if task_done(store, tid))}/{len(owned)}"
            )
        rows.append({
            "key": key,
            "position_key": f"employee-{key}",
            "name": employee.get("name", key),
            "emoji": employee.get("emoji", "👤"),
            "title": employee.get("title", ""),
            "intro": employee.get("intro", ""),
            "skills": employee.get("skills", ""),
            "tool": employee.get("tool", ""),
            "state": state,
            "state_label": state_labels[state],
            "pos": _employee_position(index),
            "short": _employee_short_name(employee),
            "sprite": _sprite_svg(shirt, hair),
            "pin_title": pin_title,
            "memory": store.memory.get(key, []),
            "owned_count": len(owned),
            "done_count": sum(1 for tid in owned if task_done(store, tid)),
            "is_manager": key == store.workflow.get("manager_key"),
            "config": _config_for_render(
                store.emp_configs.get(key, {"provider": MOCK_PROVIDER, "model": "mock-studio-model"})
            ),
        })
    return rows


def _manager_reviews(tid):
    reviews = (store.manager_reviews or {}).get(str(tid), [])
    return reviews if isinstance(reviews, list) else []


def _output_mode_values(value):
    parsed = parse_output_modes(value)
    selected = [mode for mode in OUTPUT_MODE_VALUES if mode in parsed]
    return selected or ["text"]


def _output_modes_from_form(form, field_name, fallback="text"):
    marker_name = f"{field_name}_marker"
    if field_name not in form and marker_name not in form:
        return fallback
    values = []
    for item in form.getlist(field_name):
        for mode in _output_mode_values(item):
            if mode not in values:
                values.append(mode)
    return ",".join(values or ["text"])


def _task_rows():
    employees = store.workflow.get("employees", {}) or {}
    rows = []
    for task in ordered_tasks(store.workflow):
        owner = employees.get(task.get("owner"), {})
        package = store.outputs.get(task["id"])
        text = package_text(package)
        position_seed = json.dumps({
            "title": task.get("title", ""),
            "owner": task.get("owner", ""),
            "desc": task.get("desc", ""),
            "method": task.get("method", ""),
        }, ensure_ascii=False, sort_keys=True).encode("utf-8")
        rows.append({
            **task,
            "position_key": "task-" + hashlib.sha1(position_seed).hexdigest()[:16],
            "deps_text": ", ".join(str(dep) for dep in task.get("deps", [])),
            "context_scope_label": context_scope_label(task.get("context_scope")),
            "output_mode_values": _output_mode_values(task.get("output_modes")),
            "owner_name": owner.get("name", task.get("owner")),
            "owner_emoji": owner.get("emoji", "👤"),
            "badge": _status_badge(task["id"]),
            "done": task_done(store, task["id"]),
            "ready": is_ready(store, task["id"]),
            "output": package,
            "output_text": text,
            "output_html": _markdown_html(text),
            "assets": package_assets(package),
            "reviews": _manager_reviews(task["id"]),
        })
    return rows


def _doc_rows():
    docs = []
    hist = list(store.doc_history)
    for i, doc in enumerate(reversed(hist)):
        title = doc.get("title", "Workflow Delivery")
        doc_time = doc.get("time", "")
        content = doc.get("content", "")
        assets = []
        for asset in doc.get("assets", []) if isinstance(doc.get("assets"), list) else []:
            if not isinstance(asset, dict) or not asset.get("id"):
                continue
            try:
                asset_size = int(asset.get("size") or 0)
            except (TypeError, ValueError):
                asset_size = 0
            assets.append({
                "id": str(asset.get("id")),
                "name": str(asset.get("name") or "Attachment"),
                "mime": str(asset.get("mime") or "application/octet-stream"),
                "size": asset_size,
                "task_id": asset.get("task_id"),
                "task_title": str(asset.get("task_title") or ""),
            })
        position_seed = json.dumps({
            "time": doc_time,
            "title": title,
            "content": content,
            "assets": [(asset["id"], asset["name"]) for asset in assets],
        }, ensure_ascii=False, sort_keys=True).encode("utf-8")
        docs.append({
            "display_index": len(hist) - i,
            "original_index": len(hist) - 1 - i,
            "position_key": "doc-" + hashlib.sha1(position_seed).hexdigest()[:16],
            "title": title,
            "time": doc_time,
            "content": content,
            "content_html": _markdown_html(content),
            "assets": assets,
        })
    return docs


def _workflow_template_rows():
    rows = []
    for template in reversed(list(getattr(store, "workflow_templates", []) or [])):
        workflow = normalize_workflow(template.get("workflow") or {})
        rows.append({
            "id": template.get("id", ""),
            "name": template.get("name") or workflow.get("name", "Untitled Workflow Template"),
            "description": template.get("description") or workflow.get("description", ""),
            "updated_at": template.get("updated_at", ""),
            "employee_count": len(workflow.get("employees", {}) or {}),
            "task_count": len(workflow.get("tasks", []) or []),
        })
    return rows


def _api_failure_hint_needed():
    failed_task = store.failed_task
    if not failed_task:
        return False
    output_text = package_text(store.outputs.get(failed_task))
    recent_log = "\n".join(store.log[-80:])
    text = f"{output_text}\n{recent_log}".lower()
    return any(keyword.lower() in text for keyword in API_FAILURE_HINT_KEYWORDS)


def _search_failure_hint_needed():
    failed_task = store.failed_task
    if not failed_task:
        return False
    output_text = package_text(store.outputs.get(failed_task))
    recent_log = "\n".join(store.log[-80:])
    return "web research" in f"{output_text}\n{recent_log}".lower()


def _results_revision():
    current = _current_store.get()
    with current.lock:
        payload = {
            "workflow": current.workflow,
            "outputs": current.outputs,
            "manager_reviews": current.manager_reviews,
            "running_task": current.running_task,
            "failed_task": current.failed_task,
            "is_running": current.is_running,
        }
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _render_results_panel():
    tasks = _task_rows()
    return templates.env.get_template("partials/results.html").render(
        tasks=tasks,
        done_count=sum(1 for task in tasks if task["done"]),
        store=store,
        results_revision=_results_revision(),
    )


def _runtime_pipeline_rows(tasks):
    rows = []
    for task in tasks:
        if store.running_task == task["id"]:
            status_key = "run"
        elif task["done"]:
            status_key = "done"
        elif task["ready"]:
            status_key = "ready"
        else:
            status_key = "idle"
        color, bg = PIPELINE_PALETTE[status_key]
        rows.append({
            "id": task["id"],
            "label": task.get("title") or f"Task {task['id']}",
            "owner_emoji": task["owner_emoji"],
            "owner_name": task["owner_name"],
            "color": color,
            "bg": bg,
        })
    return rows


def _runtime_employee_rows():
    return [
        {
            "key": employee["key"],
            "state": employee["state"],
            "pin_title": employee["pin_title"],
            "done_count": employee["done_count"],
            "owned_count": employee["owned_count"],
        }
        for employee in _employee_rows()
    ]


def _running_employee_label():
    emp_key = getattr(store, "running_employee", None)
    employee = (store.workflow.get("employees", {}) or {}).get(emp_key)
    if employee:
        return f"{employee.get('emoji', '👤')} {employee.get('name', emp_key)}"
    return ""


def _runtime_payload():
    tasks = _task_rows()
    dependency_issues = workflow_dependency_issues(store.workflow)
    return {
        "ok": True,
        "running": bool(store.is_running),
        "running_task": store.running_task,
        "running_employee_label": _running_employee_label(),
        "failed_task": store.failed_task,
        "api_failure_hint": _api_failure_hint_needed(),
        "search_failure_hint": _search_failure_hint_needed(),
        "done_count": sum(1 for task in tasks if task["done"]),
        "total_tasks": len(tasks),
        "employee_rows": _runtime_employee_rows(),
        "pipeline_rows": _runtime_pipeline_rows(tasks),
        "log_text": "\n".join(store.log[-200:]),
        "last_saved_at": getattr(store, "last_saved_at", ""),
        "results_revision": _results_revision(),
        "workflow_issues": dependency_issues,
    }


def _snapshot_view(tab, mode="auto", confirm_doc=None, selected_template_id=""):
    if store.is_running and not _thread_alive():
        store.mark_interrupted(store.running_task)
        store.log_line(f"⚠️ The background worker stopped. Task {store.interrupted_task or '?'} is now available to resume.")
    tasks = _task_rows()
    dependency_issues = workflow_dependency_issues(store.workflow)
    selected_template = store.get_workflow_template(selected_template_id)
    selected_template_id = selected_template.get("id") if selected_template else ""
    return {
        "tab": tab,
        "mode": mode,
        "store": store,
        "workflow": store.workflow,
        "employees": _employee_rows(),
        "tasks": tasks,
        "pipeline_rows": _runtime_pipeline_rows(tasks),
        "providers": PROVIDERS,
        "provider_labels": PROVIDER_LABELS,
        "models": MODELS,
        "global_config": _global_config_for_render(),
        "search_config": _search_config_for_render(),
        "search_providers": SEARCH_PROVIDERS,
        "search_provider_none": SEARCH_PROVIDER_NONE,
        "bing_search_retirement_message": BING_RETIREMENT_MESSAGE,
        "doc_rows": _doc_rows(),
        "workflow_templates": _workflow_template_rows(),
        "selected_workflow_template": selected_template,
        "selected_workflow_template_id": selected_template_id,
        "workflow_template_name_value": (
            selected_template.get("name") if selected_template else store.workflow.get("name", "")
        ),
        "done_count": sum(1 for task in tasks if task["done"]),
        "results_revision": _results_revision(),
        "task_max_retries": TASK_MAX_RETRIES,
        "log_text": "\n".join(store.log[-200:]),
        "per_emp_enabled": bool(getattr(store, "per_emp", False)),
        "running_employee_label": _running_employee_label(),
        "api_failure_hint": _api_failure_hint_needed(),
        "search_failure_hint": _search_failure_hint_needed(),
        "workflow_issues": dependency_issues,
        "confirm_doc": confirm_doc,
        "input_text": package_text(store.input_package),
        "input_assets": package_assets(store.input_package),
        "input_html": _markdown_html(package_text(store.input_package)),
        "office_url": "/studio_office.png",
        "context_scope_options": CONTEXT_SCOPE_OPTIONS,
        "output_mode_options": OUTPUT_MODE_OPTIONS,
    }


def _current_workflow_from_form(form, reindex=True):
    current = store.workflow
    employees = {}
    for key, old in (current.get("employees", {}) or {}).items():
        employees[key] = {
            "name": form.get(f"emp_{key}_name") if f"emp_{key}_name" in form else old.get("name", key),
            "emoji": form.get(f"emp_{key}_emoji") if f"emp_{key}_emoji" in form else old.get("emoji", "👤"),
            "title": form.get(f"emp_{key}_title") if f"emp_{key}_title" in form else old.get("title", ""),
            "intro": form.get(f"emp_{key}_intro") if f"emp_{key}_intro" in form else old.get("intro", ""),
            "skills": form.get(f"emp_{key}_skills") if f"emp_{key}_skills" in form else old.get("skills", ""),
            "tool": form.get(f"emp_{key}_tool") if f"emp_{key}_tool" in form else old.get("tool", ""),
        }
    tasks = []
    for old in ordered_tasks(current):
        tid = old["id"]
        tasks.append({
            "id": tid,
            "title": form.get(f"task_{tid}_title") if f"task_{tid}_title" in form else old.get("title", f"Task {tid}"),
            "short": form.get(f"task_{tid}_short") if f"task_{tid}_short" in form else old.get("short", f"Task {tid}"),
            "owner": form.get(f"task_{tid}_owner") if f"task_{tid}_owner" in form else old.get("owner"),
            "deps": parse_deps(form.get(f"task_{tid}_deps")) if f"task_{tid}_deps" in form else list(old.get("deps", [])),
            "desc": form.get(f"task_{tid}_desc") if f"task_{tid}_desc" in form else old.get("desc", ""),
            "method": form.get(f"task_{tid}_method") if f"task_{tid}_method" in form else old.get("method", ""),
            "acceptance": form.get(f"task_{tid}_acceptance") if f"task_{tid}_acceptance" in form else old.get("acceptance", ""),
            "output_modes": _output_modes_from_form(form, f"task_{tid}_output_modes", old.get("output_modes", "text")),
            "context_scope": form.get(f"task_{tid}_context_scope") if f"task_{tid}_context_scope" in form else old.get("context_scope", "direct_deps"),
            "web_search": form.get(f"task_{tid}_web_search") == "on",
        })
    workflow = normalize_workflow({
        "name": form.get("workflow_name") or current.get("name", ""),
        "description": form.get("workflow_description") or current.get("description", ""),
        "manager_key": form.get("manager_key") or current.get("manager_key", ""),
        "employees": employees,
        "tasks": tasks,
    })
    return _reindex_workflow_tasks(workflow) if reindex else workflow


def _reindex_workflow_tasks(workflow):
    workflow = normalize_workflow(workflow)
    old_tasks = ordered_tasks(workflow)
    id_map = {task["id"]: idx for idx, task in enumerate(old_tasks, start=1)}
    new_tasks = []
    for idx, task in enumerate(old_tasks, start=1):
        next_task = dict(task)
        next_task["id"] = idx
        deps = []
        for dep in task.get("deps", []):
            mapped = id_map.get(dep)
            if mapped and mapped != idx and mapped not in deps:
                deps.append(mapped)
        next_task["deps"] = deps
        if not str(next_task.get("short") or "").strip() or re.fullmatch(
            r"(?:Task\s*|\u4efb\u52a1)\d+",
            str(next_task.get("short", "")),
            re.IGNORECASE,
        ):
            next_task["short"] = f"Task {idx}"
        new_tasks.append(next_task)
    workflow["tasks"] = new_tasks
    return normalize_workflow(workflow)


def _employee_signature(employee):
    employee = employee or {}
    return (
        employee.get("name", ""),
        employee.get("intro", ""),
        employee.get("skills", ""),
        employee.get("tool", ""),
    )


def _task_signature(task):
    task = task or {}
    return (
        task.get("title", ""),
        task.get("owner", ""),
        tuple(task.get("deps", []) or []),
        task.get("desc", ""),
        task.get("method", ""),
        task.get("acceptance", ""),
        task.get("output_modes", ""),
        task.get("context_scope", ""),
        bool(task.get("web_search")),
    )


def _workflow_invalidated_task_ids(old_workflow, new_workflow):
    old_workflow = normalize_workflow(old_workflow)
    new_workflow = normalize_workflow(new_workflow)
    old_tasks = task_map(old_workflow)
    new_tasks = task_map(new_workflow)
    invalid = set()

    if (
        old_workflow.get("name") != new_workflow.get("name")
        or old_workflow.get("description") != new_workflow.get("description")
        or old_workflow.get("manager_key") != new_workflow.get("manager_key")
        or _employee_signature((old_workflow.get("employees") or {}).get(old_workflow.get("manager_key")))
        != _employee_signature((new_workflow.get("employees") or {}).get(new_workflow.get("manager_key")))
    ):
        return set(new_tasks)

    old_employees = old_workflow.get("employees") or {}
    new_employees = new_workflow.get("employees") or {}
    changed_employees = {
        key for key in set(old_employees) & set(new_employees)
        if _employee_signature(old_employees.get(key)) != _employee_signature(new_employees.get(key))
    }
    removed_employees = set(old_employees) - set(new_employees)
    changed_employees |= removed_employees

    for tid, task in new_tasks.items():
        old_task = old_tasks.get(tid)
        if old_task is None or _task_signature(old_task) != _task_signature(task):
            invalid.add(tid)
        if task.get("owner") in changed_employees:
            invalid.add(tid)

    with_downstream = set(invalid)
    for tid in invalid:
        with_downstream.update(downstream_task_ids(new_workflow, tid))
    return with_downstream


def _package_signature(package):
    return json.dumps(make_output_package(package), ensure_ascii=False, sort_keys=True, default=str)


def _clear_task_outputs(task_ids):
    for tid in sorted(set(task_ids)):
        store.clear_output(tid)


def _replace_input_package(package):
    old_signature = _package_signature(store.input_package)
    normalized = make_output_package(package)
    store.set_input_package(normalized)
    if _package_signature(normalized) == old_signature:
        return False
    task_ids = [task["id"] for task in ordered_tasks(store.workflow)]
    has_results = any(package_done(store.outputs.get(tid)) for tid in task_ids)
    has_reviews = bool(store.manager_reviews)
    if has_results or has_reviews:
        _clear_task_outputs(task_ids)
    return True


def _public_input_assets():
    fields = (
        "id", "name", "mime", "size", "extraction_status",
        "extracted_chars", "extraction_note", "note",
    )
    return [
        {field: asset.get(field) for field in fields if asset.get(field) not in (None, "")}
        for asset in package_assets(store.input_package)
    ]


async def _assets_from_form(form, field_name):
    assets = []
    for key, value in form.multi_items():
        if key != field_name:
            continue
        if not (hasattr(value, "filename") and hasattr(value, "read")):
            continue
        if not value.filename:
            continue
        raw = await value.read()
        if not raw:
            continue
        if len(raw) > MAX_ASSET_BYTES:
            assets.append({
                "id": uuid.uuid4().hex,
                "name": value.filename,
                "mime": "text/plain",
                "size": 0,
                "data": base64.b64encode(f"Attachment exceeded {MAX_ASSET_BYTES} bytes and was not saved.".encode("utf-8")).decode("ascii"),
                "note": "The attachment exceeded the size limit. The original content was not saved.",
            })
            continue
        assets.append({
            "id": uuid.uuid4().hex,
            "name": value.filename,
            "mime": value.content_type or "application/octet-stream",
            "size": len(raw),
            "data": base64.b64encode(raw).decode("ascii"),
            **extract_file_content(value.filename, value.content_type, raw),
        })
    return assets[:MAX_ASSETS_PER_PACKAGE]


async def _package_from_form(form, text_field, file_field, existing_package=None, remove_field=None):
    text = str(form.get(text_field, "") or "")
    existing_assets = package_assets(existing_package)
    remove_ids = set(form.getlist(remove_field)) if remove_field else set()
    kept = [asset for asset in existing_assets if asset.get("id") not in remove_ids]
    new_assets = await _assets_from_form(form, file_field)
    return make_output_package(text, assets=(kept + new_assets)[:MAX_ASSETS_PER_PACKAGE])


def _asset_response(asset, fallback):
    if not asset:
        return PlainTextResponse("Attachment not found", status_code=404)
    try:
        data = base64.b64decode(asset.get("data") or "")
    except Exception:
        return PlainTextResponse("Attachment data is corrupted", status_code=500)
    return Response(
        data,
        media_type=asset.get("mime") or "application/octet-stream",
        headers=_download_headers(asset.get("name") or fallback, fallback),
    )


@app.get("/", response_class=HTMLResponse)
def index(request: Request, tab: str = "studio", mode: str = "auto", confirm_doc: int = None,
          workflow_template_id: str = ""):
    if tab not in {"studio", "settings", "docs"}:
        tab = "studio"
    if mode not in {"auto", "manual"}:
        mode = "auto"
    return templates.TemplateResponse(
        "app.html",
        {"request": request, **_snapshot_view(
            tab,
            mode,
            confirm_doc=confirm_doc,
            selected_template_id=workflow_template_id,
        )},
    )


@app.get("/studio_office.png")
def studio_office():
    return FileResponse(PROJECT_ROOT / "studio_office.png")


@app.post("/config")
async def save_config(request: Request):
    if store.is_running:
        return _redirect("studio")
    form = await request.form()
    old_configs = dict(store.emp_configs or {})
    old_global = getattr(store, "global_config", None)
    if not isinstance(old_global, dict):
        old_global = {}
    global_provider = _valid_provider(form.get("global_provider"), old_global.get("provider", MOCK_PROVIDER))
    global_model = _model_from_form(
        global_provider,
        form.get("global_selected_model"),
        form.get("global_custom_model"),
        rendered_provider=form.get("global_rendered_provider") or global_provider,
    )
    global_key = str(form.get("global_key", old_global.get("key", "")) or "")
    global_config = {"provider": global_provider, "key": global_key, "model": global_model}
    old_search = getattr(store, "search_config", None)
    old_search = dict(old_search) if isinstance(old_search, dict) else {}
    search_provider = normalize_search_provider(
        form.get("search_provider"),
        old_search.get("provider", SEARCH_PROVIDER_NONE),
    )
    rendered_search_provider = normalize_search_provider(
        form.get("search_rendered_provider"),
        search_provider,
    )
    if (
        search_provider == SEARCH_PROVIDER_NONE
        or search_provider == "Bing Search"
        or rendered_search_provider != search_provider
    ):
        search_key = ""
    else:
        search_key = str(form.get("search_key", old_search.get("key", "")) or "")
    search_config = {"provider": search_provider, "key": search_key}
    per_emp = form.get("per_emp") == "on"
    emp_configs = {}
    for key in store.workflow.get("employees", {}):
        old = old_configs.get(key, {})
        if per_emp and f"emp_{key}_provider" in form:
            provider = _valid_provider(form.get(f"emp_{key}_provider"), old.get("provider", global_provider))
            model = _model_from_form(
                provider,
                form.get(f"emp_{key}_selected_model"),
                form.get(f"emp_{key}_custom_model"),
                rendered_provider=form.get(f"emp_{key}_rendered_provider") or provider,
                fallback_model=global_model if provider == global_provider else "",
            )
            api_key = str(form.get(f"emp_{key}_key", old.get("key", "") or global_key) or "")
        else:
            provider = global_provider
            model = global_model
            api_key = global_key
        emp_configs[key] = {"provider": provider, "key": api_key, "model": model}
    store.snapshot_config(
        emp_configs=emp_configs,
        per_emp=per_emp,
        global_config=global_config,
        search_config=search_config,
    )
    return _redirect(form.get("return_tab") or "studio")


@app.post("/input")
async def save_input(request: Request):
    if store.is_running:
        return _redirect("studio")
    form = await request.form()
    package = await _package_from_form(
        form,
        "input_text",
        "input_files",
        existing_package=store.input_package,
        remove_field="remove_input_asset",
    )
    _replace_input_package(package)
    return _redirect("studio")


@app.post("/api/input/text")
async def autosave_input_text(request: Request):
    if store.is_running:
        return JSONResponse({"ok": False, "error": "Task input cannot be changed while the workflow is running."}, status_code=409)
    form = await request.form()
    text = str(form.get("input_text", "") or "")
    with store.lock:
        package = make_output_package(text, assets=package_assets(store.input_package))
        changed = _replace_input_package(package)
    return {"ok": True, "changed": changed, "input_text": package_text(store.input_package)}


@app.post("/api/input/assets")
async def autosave_input_assets(request: Request):
    if store.is_running:
        return JSONResponse({"ok": False, "error": "Attachments cannot be uploaded while the workflow is running."}, status_code=409)
    form = await request.form()
    new_assets = await _assets_from_form(form, "input_files")
    if not new_assets:
        return JSONResponse({"ok": False, "error": "No valid attachments were selected."}, status_code=400)
    with store.lock:
        current_assets = package_assets(store.input_package)
        available = max(0, MAX_ASSETS_PER_PACKAGE - len(current_assets))
        accepted = new_assets[:available]
        if not accepted:
            return JSONResponse(
                {"ok": False, "error": f"You can keep up to {MAX_ASSETS_PER_PACKAGE} reference attachments."},
                status_code=400,
            )
        package = make_output_package(
            package_text(store.input_package),
            assets=current_assets + accepted,
        )
        _replace_input_package(package)
    return {
        "ok": True,
        "added_count": len(accepted),
        "assets": _public_input_assets(),
    }


@app.post("/api/input/assets/{asset_id}/delete")
def autosave_delete_input_asset(asset_id: str):
    if store.is_running:
        return JSONResponse({"ok": False, "error": "Attachments cannot be deleted while the workflow is running."}, status_code=409)
    with store.lock:
        current_assets = package_assets(store.input_package)
        kept_assets = [asset for asset in current_assets if asset.get("id") != asset_id]
        if len(kept_assets) == len(current_assets):
            return JSONResponse({"ok": False, "error": "The attachment was not found or has already been deleted."}, status_code=404)
        package = make_output_package(package_text(store.input_package), assets=kept_assets)
        _replace_input_package(package)
    return {"ok": True, "assets": _public_input_assets()}


@app.post("/settings")
async def save_settings(request: Request):
    if store.is_running:
        return _redirect("settings")
    form = await request.form()
    if form.get("settings_form_marker") != "1":
        store.log_line("⚠️ The Company Setup request was missing its complete-form marker and was ignored to protect the current configuration.")
        return _redirect("settings")
    old_workflow = normalize_workflow(store.workflow)
    new_workflow = _current_workflow_from_form(form)
    invalidated = _workflow_invalidated_task_ids(old_workflow, new_workflow)
    store.snapshot_config(workflow=new_workflow)
    _clear_task_outputs(invalidated)
    return _redirect("settings")


@app.post("/settings/template/save")
async def save_workflow_template(request: Request):
    if store.is_running:
        return _redirect("settings")
    form = await request.form()
    if form.get("settings_form_marker") != "1":
        store.log_line("⚠️ The template request was missing its complete-form marker and was ignored to prevent an incomplete save.")
        return _redirect("settings")
    old_workflow = normalize_workflow(store.workflow)
    new_workflow = _current_workflow_from_form(form)
    selected_template = store.get_workflow_template(form.get("workflow_template_id"))
    name = str(
        form.get("workflow_template_name")
        or (selected_template or {}).get("name")
        or new_workflow.get("name")
        or "Untitled Workflow Template"
    ).strip()
    entry = store.save_workflow_template(name=name, workflow=new_workflow)
    saved_workflow = entry.get("workflow") or new_workflow
    invalidated = _workflow_invalidated_task_ids(old_workflow, saved_workflow)
    store.snapshot_config(workflow=saved_workflow)
    _clear_task_outputs(invalidated)
    store.log_line(f"💾 Saved as a new workflow template: {entry.get('name')}. The original template was not overwritten.")
    return _redirect("settings", workflow_template_id=entry.get("id"))


@app.post("/settings/template/new")
async def new_workflow_template(request: Request):
    if store.is_running:
        return _redirect("settings")
    old_workflow = normalize_workflow(store.workflow)
    new_workflow = clone_default_workflow()
    new_workflow["name"] = "Untitled Workflow Template"
    new_workflow["description"] = ""
    new_workflow = _reindex_workflow_tasks(new_workflow)
    invalidated = _workflow_invalidated_task_ids(old_workflow, new_workflow)
    store.snapshot_config(workflow=new_workflow)
    _clear_task_outputs(invalidated)
    store.log_line("🆕 Opened a new workflow template draft. Configure the agents and tasks, then select Save as New Template.")
    return _redirect("settings")


@app.post("/settings/template/apply")
async def apply_workflow_template(request: Request):
    if store.is_running:
        return _redirect("settings")
    form = await request.form()
    template_id = str(form.get("workflow_template_id") or "").strip()
    template = store.get_workflow_template(template_id)
    if not template:
        store.log_line("⚠️ Select a valid workflow template before applying it.")
        return _redirect("settings")
    old_workflow = normalize_workflow(store.workflow)
    new_workflow = _reindex_workflow_tasks(template.get("workflow") or {})
    invalidated = _workflow_invalidated_task_ids(old_workflow, new_workflow)
    store.snapshot_config(workflow=new_workflow)
    _clear_task_outputs(invalidated)
    store.log_line(f"📋 Applied workflow template: {template.get('name') or new_workflow.get('name')}.")
    return _redirect("settings", workflow_template_id=template_id)


@app.post("/settings/template/delete")
async def delete_workflow_template(request: Request):
    if store.is_running:
        return _redirect("settings")
    form = await request.form()
    template_id = form.get("workflow_template_id")
    template = store.get_workflow_template(template_id)
    if store.delete_workflow_template(template_id):
        store.log_line(f"🗑 Deleted workflow template: {(template or {}).get('name', template_id)}.")
    else:
        store.log_line("⚠️ Select a valid workflow template before deleting it.")
    return _redirect("settings")


@app.post("/settings/reset")
async def reset_workflow_settings(request: Request):
    form = await request.form()
    if not store.is_running and form.get("confirm_reset") == "1":
        store.snapshot_config(workflow=normalize_workflow(clone_default_workflow()))
        _clear_task_outputs(task["id"] for task in ordered_tasks(store.workflow))
        store.log_line("↩︎ Restored the default workflow and cleared previous task outputs.")
    elif not store.is_running:
        store.log_line("⚠️ The default-template reset was ignored because the confirmation marker was missing.")
    return _redirect("settings")


@app.post("/settings/employee/add")
async def add_employee(request: Request):
    if store.is_running:
        return _redirect("settings")
    form = await request.form()
    old_workflow = normalize_workflow(store.workflow)
    workflow = _current_workflow_from_form(form) if form.get("settings_form_marker") == "1" else old_workflow
    name = str(form.get("employee_name") or "New AI Agent").strip()
    key = make_employee_key(name, workflow.get("employees", {}))
    workflow["employees"][key] = {
        "name": name,
        "emoji": str(form.get("employee_emoji") or "👤").strip()[:4],
        "title": "Custom AI Agent",
        "intro": "Describe this agent's role, relevant expertise, and operating boundaries.",
        "skills": "Understand requirements\nExecute tasks\nSynthesize results",
        "tool": "Custom tools / capabilities",
    }
    invalidated = _workflow_invalidated_task_ids(old_workflow, workflow)
    store.snapshot_config(workflow=workflow)
    _clear_task_outputs(invalidated)
    store.log_line(f"➕ Added AI agent: {name}.")
    return _redirect("settings")


@app.post("/settings/employee/delete/{emp_key}")
async def delete_employee(emp_key: str, request: Request):
    if store.is_running:
        return _redirect("settings")
    form = await request.form()
    old_workflow = normalize_workflow(store.workflow)
    workflow = _current_workflow_from_form(form) if form.get("settings_form_marker") == "1" else old_workflow
    employees = workflow.get("employees", {})
    if emp_key in employees and len(employees) > 1:
        employees.pop(emp_key, None)
        fallback = next(iter(employees))
        for task in workflow.get("tasks", []):
            if task.get("owner") == emp_key:
                task["owner"] = fallback
        if workflow.get("manager_key") == emp_key:
            workflow["manager_key"] = fallback
        invalidated = _workflow_invalidated_task_ids(old_workflow, workflow)
        store.snapshot_config(workflow=workflow)
        _clear_task_outputs(invalidated)
        store.log_line(f"🗑 Deleted AI agent {emp_key}. Reassigned its tasks to {fallback}.")
    return _redirect("settings")


@app.post("/settings/task/add")
async def add_task(request: Request):
    if store.is_running:
        return _redirect("settings")
    form = await request.form()
    old_workflow = normalize_workflow(store.workflow)
    workflow = _current_workflow_from_form(form) if form.get("settings_form_marker") == "1" else old_workflow
    tid = next_task_id(workflow)
    employees = workflow.get("employees", {})
    manager = workflow.get("manager_key")
    owner = next((key for key in employees if key != manager), next(iter(employees), ""))
    title = str(form.get("task_title") or f"New Task {tid}").strip()
    new_task = {
        "id": tid,
        "title": title,
        "short": f"Task {tid}",
        "owner": owner,
        "deps": [],
        "desc": "Describe what this task must accomplish.",
        "method": "Describe the working method, execution steps, and key considerations.",
        "acceptance": "Define the acceptance criteria this task must meet.",
        "output_modes": "text",
        "web_search": False,
    }
    new_task["context_scope"] = infer_context_scope(new_task, is_last=True)
    workflow["tasks"].append(new_task)
    workflow = _reindex_workflow_tasks(workflow)
    invalidated = _workflow_invalidated_task_ids(old_workflow, workflow)
    store.snapshot_config(workflow=workflow)
    _clear_task_outputs(invalidated)
    store.log_line(f"➕ Added Task {tid}.")
    return _redirect("settings")


@app.post("/settings/task/delete/{tid}")
async def delete_task(tid: int, request: Request):
    if store.is_running:
        return _redirect("settings")
    old_task_ids = [task["id"] for task in ordered_tasks(store.workflow)]
    if len(old_task_ids) <= 1:
        store.log_line("⚠️ The workflow must contain at least one task. The delete request was ignored.")
        return _redirect("settings")
    form = await request.form()
    workflow = _current_workflow_from_form(form, reindex=False) if form.get("settings_form_marker") == "1" else normalize_workflow(store.workflow)
    workflow["tasks"] = [task for task in workflow.get("tasks", []) if task["id"] != tid]
    for task in workflow["tasks"]:
        task["deps"] = [dep for dep in task.get("deps", []) if dep != tid]
    workflow = _reindex_workflow_tasks(workflow)
    store.snapshot_config(workflow=workflow)
    new_task_ids = [task["id"] for task in ordered_tasks(store.workflow)]
    _clear_task_outputs([*old_task_ids, *new_task_ids])
    store.log_line(f"🗑 Deleted Task {tid}, renumbered the remaining tasks, and cleared previous outputs.")
    return _redirect("settings")


@app.post("/settings/task/{tid}/output-modes")
async def save_task_output_modes(tid: int, request: Request):
    if store.is_running:
        return JSONResponse({"ok": False, "error": "Output modalities cannot be changed while the workflow is running."}, status_code=409)
    current_task = task_map(store.workflow).get(tid)
    if not current_task:
        return JSONResponse({"ok": False, "error": "Task not found."}, status_code=404)

    form = await request.form()
    output_modes = _output_modes_from_form(form, "output_modes", current_task.get("output_modes", "text"))
    old_workflow = normalize_workflow(store.workflow)
    new_workflow = normalize_workflow(old_workflow)
    for task in new_workflow.get("tasks", []):
        if task["id"] == tid:
            task["output_modes"] = output_modes
            break

    invalidated = _workflow_invalidated_task_ids(old_workflow, new_workflow)
    store.snapshot_config(workflow=new_workflow)
    _clear_task_outputs(invalidated)
    return {
        "ok": True,
        "output_modes": output_modes,
        "output_mode_values": _output_mode_values(output_modes),
    }


@app.post("/run/all")
def run_all(request: Request):
    started = False
    if _workflow_ready_to_run():
        started = _start_background(run_pipeline, store, from_progress=False)
    if request.headers.get("X-Requested-With") == "fetch":
        issues = workflow_dependency_issues(store.workflow)
        return JSONResponse(
            {
                "ok": bool(started or store.is_running),
                "started": bool(started),
                "running": bool(store.is_running),
                "error": "" if started or store.is_running else (issues[0] if issues else "The workflow could not start. Try again in a moment."),
            },
            status_code=200 if started or store.is_running else 409,
        )
    return _redirect("studio")


@app.post("/run/rest")
def run_rest():
    if _workflow_ready_to_run():
        _start_background(run_pipeline, store, from_progress=True)
    return _redirect("studio", mode="manual")


@app.post("/run/task/{tid}")
def run_one_task(tid: int):
    if tid in task_map(store.workflow) and is_ready(store, tid):
        _start_background(_run_single_task, tid)
    return _redirect("studio", mode="manual")


@app.post("/stop")
def stop_run():
    bound_store = _current_store.get()
    worker = bound_store.thread
    store.force_stop()
    if worker is not None and worker is not threading.current_thread() and worker.is_alive():
        worker.join(timeout=1.5)
    return _redirect("studio")


@app.post("/reset")
def reset_run():
    store.reset()
    return _redirect("studio")


@app.post("/resume")
def resume_run():
    store.clear_interrupted()
    if _workflow_ready_to_run():
        _start_background(run_pipeline, store, from_progress=True)
    return _redirect("studio")


@app.post("/dismiss-interrupted")
def dismiss_interrupted():
    store.clear_interrupted()
    return _redirect("studio")


@app.post("/task/{tid}/save")
async def save_task_output(tid: int, request: Request):
    if store.is_running or tid not in task_map(store.workflow):
        return _redirect("studio", mode="manual")
    form = await request.form()
    package = await _package_from_form(
        form,
        "content",
        "output_files",
        existing_package=store.outputs.get(tid),
        remove_field="remove_output_asset",
    )
    store.set_output(tid, package)
    store.clear_manager_review(str(tid))
    _invalidate_downstream(tid)
    _start_background(_run_manual_task_review, tid, package)
    return _redirect("studio", mode="manual")


@app.post("/task/{tid}/clear")
def clear_task_output(tid: int):
    if not store.is_running and tid in task_map(store.workflow):
        store.clear_output(tid)
        _invalidate_downstream(tid)
    return _redirect("studio", mode="manual")


@app.post("/docs/clear")
def clear_docs():
    if not store.is_running:
        store.clear_doc_history()
    return _redirect("docs")


@app.post("/docs/delete/{index}")
def delete_doc(index: int):
    if not store.is_running:
        store.delete_doc_history(index)
    return _redirect("docs")


@app.get("/download/task/{tid}")
def download_task(tid: int):
    task = task_map(store.workflow).get(tid)
    content = package_text(store.outputs.get(tid))
    filename = f"task{tid}_{task.get('short', 'output') if task else 'output'}.md"
    return Response(
        content,
        media_type="text/markdown; charset=utf-8",
        headers=_download_headers(filename, f"task{tid}_output.md"),
    )


@app.get("/download/task/{tid}/asset/{asset_id}")
def download_task_asset(tid: int, asset_id: str):
    for asset in package_assets(store.outputs.get(tid)):
        if asset.get("id") == asset_id:
            return _asset_response(asset, "asset.bin")
    return PlainTextResponse("Attachment not found", status_code=404)


@app.get("/download/input/asset/{asset_id}")
def download_input_asset(asset_id: str):
    for asset in package_assets(store.input_package):
        if asset.get("id") == asset_id:
            return _asset_response(asset, "input_asset.bin")
    return PlainTextResponse("Attachment not found", status_code=404)


@app.get("/download/final")
def download_final():
    content = compile_delivery_doc(store)
    return Response(
        content,
        media_type="text/markdown; charset=utf-8",
        headers=_download_headers(f"{store.workflow.get('name', 'workflow')}_final.md", "workflow_final.md"),
    )


@app.get("/download/doc/{index}")
def download_doc(index: int):
    docs = store.doc_history if isinstance(store.doc_history, list) else []
    if not (0 <= index < len(docs)):
        return PlainTextResponse("Document not found", status_code=404)
    doc = docs[index]
    content = doc.get("content", "") if isinstance(doc, dict) else ""
    title = doc.get("title", "Workflow Delivery") if isinstance(doc, dict) else "Workflow Delivery"
    return Response(
        content,
        media_type="text/markdown; charset=utf-8",
        headers=_download_headers(f"{title}.md", f"workflow_doc_{index + 1}.md"),
    )


@app.get("/download/doc/{index}/asset/{asset_id}")
def download_doc_asset(index: int, asset_id: str):
    docs = store.doc_history if isinstance(store.doc_history, list) else []
    if not (0 <= index < len(docs)):
        return PlainTextResponse("Document not found", status_code=404)
    doc = docs[index]
    assets = doc.get("assets", []) if isinstance(doc, dict) else []
    for asset in assets if isinstance(assets, list) else []:
        if isinstance(asset, dict) and str(asset.get("id") or "") == asset_id:
            return _asset_response(asset, "archive_asset.bin")
    return PlainTextResponse("Archived attachment not found", status_code=404)


@app.get("/health")
def health():
    return {"ok": True, "running": store.is_running}


@app.get("/partials/results", response_class=HTMLResponse)
def partial_results():
    return HTMLResponse(_render_results_panel())


@app.get("/api/runtime")
def api_runtime():
    return _runtime_payload()
