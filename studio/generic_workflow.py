# -*- coding: utf-8 -*-
"""Data structures and pure-logic utilities for configurable agent workflows.

The FastAPI product uses this module to support general-purpose workflows:
- Users can add, remove, and customize AI agents, roles, and skills.
- Users can configure tasks, dependencies, owners, methods, and acceptance criteria.
- Inputs and outputs use a shared content package with text and attachments.
"""

import copy
import re
import time


DEFAULT_MANAGER_KEY = "manager"
LEGACY_ALL_OUTPUT_MODES = {"text", "file", "image", "audio", "video"}
MAX_PROMPT_EXTRACTED_CHARS = 60000
CONTEXT_SCOPE_DIRECT_DEPS = "direct_deps"
CONTEXT_SCOPE_UPSTREAM_DEPS = "upstream_deps"
CONTEXT_SCOPE_PREVIOUS_TASKS = "previous_tasks"
CONTEXT_SCOPE_ALL_TASKS = "all_tasks"
CONTEXT_SCOPE_OPTIONS = [
    (CONTEXT_SCOPE_DIRECT_DEPS, "Direct dependencies only"),
    (CONTEXT_SCOPE_UPSTREAM_DEPS, "All upstream dependencies"),
    (CONTEXT_SCOPE_PREVIOUS_TASKS, "All completed earlier tasks"),
    (CONTEXT_SCOPE_ALL_TASKS, "All completed tasks"),
]
CONTEXT_SCOPE_LABELS = dict(CONTEXT_SCOPE_OPTIONS)
CONTEXT_SCOPE_ALIASES = {
    "direct": CONTEXT_SCOPE_DIRECT_DEPS,
    "deps": CONTEXT_SCOPE_DIRECT_DEPS,
    "direct_deps": CONTEXT_SCOPE_DIRECT_DEPS,
    "\u76f4\u63a5\u4f9d\u8d56": CONTEXT_SCOPE_DIRECT_DEPS,
    "\u4ec5\u76f4\u63a5\u4f9d\u8d56\u4efb\u52a1": CONTEXT_SCOPE_DIRECT_DEPS,
    "upstream": CONTEXT_SCOPE_UPSTREAM_DEPS,
    "upstream_deps": CONTEXT_SCOPE_UPSTREAM_DEPS,
    "ancestors": CONTEXT_SCOPE_UPSTREAM_DEPS,
    "\u5168\u90e8\u4e0a\u6e38": CONTEXT_SCOPE_UPSTREAM_DEPS,
    "\u5168\u90e8\u4e0a\u6e38\u7956\u5148\u4efb\u52a1": CONTEXT_SCOPE_UPSTREAM_DEPS,
    "previous": CONTEXT_SCOPE_PREVIOUS_TASKS,
    "previous_tasks": CONTEXT_SCOPE_PREVIOUS_TASKS,
    "all_previous": CONTEXT_SCOPE_PREVIOUS_TASKS,
    "\u524d\u5e8f\u4efb\u52a1": CONTEXT_SCOPE_PREVIOUS_TASKS,
    "\u5168\u90e8\u5df2\u5b8c\u6210\u524d\u5e8f\u4efb\u52a1": CONTEXT_SCOPE_PREVIOUS_TASKS,
    "all": CONTEXT_SCOPE_ALL_TASKS,
    "all_tasks": CONTEXT_SCOPE_ALL_TASKS,
    "all_done": CONTEXT_SCOPE_ALL_TASKS,
    "\u5168\u90e8\u4efb\u52a1": CONTEXT_SCOPE_ALL_TASKS,
    "\u5168\u90e8\u5df2\u5b8c\u6210\u4efb\u52a1": CONTEXT_SCOPE_ALL_TASKS,
}
SUMMARY_TASK_KEYWORDS = (
    "final", "summary", "consolidate", "deliver", "package", "document",
    "archive", "publish", "wrap-up", "final review",
)

DEFAULT_WORKFLOW = {
    "name": "AI Agent Collaboration Workspace",
    "description": "A configurable team of AI agents that collaborates across a task workflow to achieve your business objective.",
    "manager_key": DEFAULT_MANAGER_KEY,
    "employees": {
        "manager": {
            "name": "Workflow Manager",
            "emoji": "🧭",
            "title": "Orchestration / Quality Gates / Revision Loops",
            "intro": "Interprets the user's objective, assigns the right AI agents, and reviews each stage against its acceptance criteria. When a deliverable is incomplete, inconsistent, or insufficient for downstream work, provides specific revision guidance and initiates another pass.",
            "skills": "Task decomposition\nDependency orchestration\nQuality assurance\nRevision management\nFinal delivery approval",
            "tool": "Workflow orchestration and quality assurance",
        },
        "analyst": {
            "name": "Requirements Analyst",
            "emoji": "🔎",
            "title": "Goal Discovery / Information Synthesis / Problem Framing",
            "intro": "Reviews user input and attachments to identify objectives, constraints, context, success criteria, and risks, then turns an ambiguous request into a clear problem statement.",
            "skills": "Requirements clarification\nInformation extraction\nConstraint analysis\nRisk identification\nStructured communication",
            "tool": "Requirements analysis and information synthesis",
        },
        "planner": {
            "name": "Solution Architect",
            "emoji": "🧩",
            "title": "Solution Design / Execution Planning / Decision Frameworks",
            "intro": "Designs an actionable solution based on the requirements analysis, including key steps, tradeoffs, milestones, and the delivery structure.",
            "skills": "Solution design\nWorkflow planning\nSystems thinking\nOption analysis\nExecution roadmapping",
            "tool": "Solution design and execution planning",
        },
        "executor": {
            "name": "Execution Specialist",
            "emoji": "⚙️",
            "title": "Content Generation / Task Execution / Deliverable Production",
            "intro": "Produces the core deliverables from the approved plan and upstream context, with an emphasis on completeness, clarity, actionability, and reuse.",
            "skills": "Deliverable creation\nDetail development\nMulti-format communication\nHands-on execution\nResult synthesis",
            "tool": "Core task execution",
        },
        "reviewer": {
            "name": "Quality Reviewer",
            "emoji": "✅",
            "title": "Quality Review / Consistency Checks / Risk Validation",
            "intro": "Reviews each deliverable against the task objective, acceptance criteria, and upstream context to identify logic gaps, omissions, formatting issues, and risks.",
            "skills": "Logic review\nConsistency validation\nFormat review\nEdge-case analysis\nRisk assessment",
            "tool": "Quality review and revision guidance",
        },
    },
    "tasks": [
        {
            "id": 1,
            "title": "Understand the Objective and Source Material",
            "short": "Goal Analysis",
            "owner": "analyst",
            "deps": [],
            "desc": "Review the user's text, images, files, and other source material. Identify the objective, context, constraints, success criteria, and open questions.",
            "method": "Extract facts first, then clearly separate explicit requirements from reasonable inferences. Use a structured format and avoid unsupported assumptions.",
            "acceptance": "Must include the objective, background, source-material summary, constraints, risks, and open questions. Clearly identify any missing information.",
            "output_modes": "text",
        },
        {
            "id": 2,
            "title": "Design the Solution and Execution Plan",
            "short": "Solution Plan",
            "owner": "planner",
            "deps": [1],
            "desc": "Use the Task 1 analysis to create an actionable solution with steps, resource dependencies, risk mitigations, and a delivery structure.",
            "method": "Prioritize a practical, reliable approach. Explain the rationale and impact of every material tradeoff.",
            "acceptance": "Must include execution steps, key decisions, risk mitigations, delivery format, and quality standards.",
            "output_modes": "text",
        },
        {
            "id": 3,
            "title": "Produce the Core Deliverable",
            "short": "Core Output",
            "owner": "executor",
            "deps": [1, 2],
            "desc": "Execute the primary work defined in the plan and produce the core result the user needs.",
            "method": "Follow the upstream analysis and plan, preserve every material constraint, and produce a complete, clear, ready-to-use deliverable.",
            "acceptance": "The core deliverable must address the user's objective and plan requirements, use a clear structure, and be ready for review.",
            "output_modes": "text",
        },
        {
            "id": 4,
            "title": "Review Quality and Recommend Improvements",
            "short": "Quality Review",
            "owner": "reviewer",
            "deps": [3],
            "desc": "Review the core deliverable for completeness, consistency, accuracy, formatting, and potential risks, then provide actionable revision guidance.",
            "method": "Evaluate objective coverage, logical coherence, edge cases, failure scenarios, and delivery format.",
            "acceptance": "Must include a review decision, issue list, severity levels, and recommended fixes. If no issues are found, explain the basis for approval.",
            "output_modes": "text",
        },
        {
            "id": 5,
            "title": "Create the Final Deliverable",
            "short": "Final Delivery",
            "owner": "executor",
            "deps": [3, 4],
            "desc": "Incorporate the review feedback and prepare a complete final version for the user.",
            "method": "Address each review item, retain any necessary context, and ensure the final result is coherent, complete, and consistently formatted.",
            "acceptance": "The final result must address the user's objective, account for the review feedback, and provide a ready-to-use deliverable.",
            "output_modes": "text",
        },
    ],
}


def clone_default_workflow():
    return copy.deepcopy(DEFAULT_WORKFLOW)


def _clean_text(value, fallback=""):
    text = str(value or "").strip()
    return text if text else fallback


def _clean_bool(value, fallback=False):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value or "").strip().lower()
    if text in {"1", "true", "yes", "on", "\u542f\u7528", "\u5f00\u542f"}:
        return True
    if text in {"0", "false", "no", "off", "\u7981\u7528", "\u5173\u95ed"}:
        return False
    return bool(fallback)


def normalize_context_scope(value, fallback=CONTEXT_SCOPE_DIRECT_DEPS):
    text = str(value or "").strip()
    if not text:
        return fallback
    normalized = CONTEXT_SCOPE_ALIASES.get(text.lower()) or CONTEXT_SCOPE_ALIASES.get(text)
    return normalized or (text if text in CONTEXT_SCOPE_LABELS else fallback)


def context_scope_label(value):
    scope = normalize_context_scope(value)
    return CONTEXT_SCOPE_LABELS.get(scope, CONTEXT_SCOPE_LABELS[CONTEXT_SCOPE_DIRECT_DEPS])


def _looks_like_summary_task(task):
    text = f"{task.get('title', '')} {task.get('short', '')} {task.get('desc', '')} {task.get('method', '')}"
    return any(keyword in text for keyword in SUMMARY_TASK_KEYWORDS)


def infer_context_scope(task, is_last=False):
    if is_last and _looks_like_summary_task(task or {}):
        return CONTEXT_SCOPE_PREVIOUS_TASKS
    return CONTEXT_SCOPE_DIRECT_DEPS


def make_employee_key(name, existing=None):
    existing = set(existing or [])
    raw = re.sub(r"[^0-9A-Za-z_\u4e00-\u9fff]+", "_", str(name or "employee")).strip("_")
    if not raw:
        raw = "employee"
    # Configuration keys are used in form names and URLs, so ASCII is safest.
    ascii_key = re.sub(r"[^0-9A-Za-z_]+", "", raw)
    base = ascii_key.lower() or "employee"
    if not re.match(r"^[A-Za-z_]", base):
        base = "employee_" + base
    key = base
    idx = 2
    while key in existing:
        key = f"{base}_{idx}"
        idx += 1
    return key


def normalize_employee(key, value):
    value = value if isinstance(value, dict) else {}
    name = _clean_text(value.get("name"), key)
    return {
        "name": name,
        "emoji": _clean_text(value.get("emoji"), "👤")[:4],
        "title": _clean_text(value.get("title"), "AI Agent"),
        "intro": _clean_text(value.get("intro"), "Completes the tasks assigned to this agent."),
        "skills": _clean_text(value.get("skills"), "Understand requirements\nExecute tasks\nSynthesize results"),
        "tool": _clean_text(value.get("tool"), "General task execution"),
    }


def normalize_task(raw, employees, fallback_id):
    raw = raw if isinstance(raw, dict) else {}
    try:
        tid = int(raw.get("id", fallback_id))
    except (TypeError, ValueError):
        tid = fallback_id
    owner = _clean_text(raw.get("owner"), "")
    if owner not in employees:
        owner = next(iter(employees), "")
    deps = []
    for item in raw.get("deps", []):
        try:
            dep = int(item)
        except (TypeError, ValueError):
            continue
        if dep != tid and dep not in deps:
            deps.append(dep)
    output_modes = _clean_text(raw.get("output_modes"), "text")
    parsed_modes = parse_output_modes(output_modes)
    if parsed_modes == LEGACY_ALL_OUTPUT_MODES:
        output_modes = "text"
    context_scope = normalize_context_scope(raw.get("context_scope"), "")
    return {
        "id": tid,
        "title": _clean_text(raw.get("title"), f"Task {tid}"),
        "short": _clean_text(raw.get("short"), f"Task {tid}")[:20],
        "owner": owner,
        "deps": deps,
        "desc": _clean_text(raw.get("desc"), "Complete this task and provide the requested output."),
        "method": _clean_text(raw.get("method"), "Work from the objective, inputs, constraints, and acceptance criteria."),
        "acceptance": _clean_text(raw.get("acceptance"), "The output must be complete, clear, and sufficient for downstream tasks."),
        "output_modes": output_modes,
        "context_scope": context_scope,
        "web_search": _clean_bool(raw.get("web_search"), False),
    }


def normalize_workflow(workflow):
    workflow = workflow if isinstance(workflow, dict) else {}
    employees = workflow.get("employees") if isinstance(workflow.get("employees"), dict) else {}
    clean_employees = {}
    for key, value in employees.items():
        safe_key = make_employee_key(key, clean_employees)
        clean_employees[safe_key] = normalize_employee(safe_key, value)
    if not clean_employees:
        clean_employees = clone_default_workflow()["employees"]

    manager_key = _clean_text(workflow.get("manager_key"), DEFAULT_MANAGER_KEY)
    if manager_key not in clean_employees:
        manager_key = next(iter(clean_employees), "")

    raw_tasks = workflow.get("tasks") if isinstance(workflow.get("tasks"), list) else []
    clean_tasks = []
    used_ids = set()
    for idx, raw in enumerate(raw_tasks, start=1):
        task = normalize_task(raw, clean_employees, idx)
        if task["id"] in used_ids:
            task["id"] = next_task_id({"tasks": clean_tasks})
        used_ids.add(task["id"])
        clean_tasks.append(task)
    if not clean_tasks:
        clean_tasks = clone_default_workflow()["tasks"]

    valid_ids = {task["id"] for task in clean_tasks}
    for task in clean_tasks:
        task["deps"] = [dep for dep in task["deps"] if dep in valid_ids and dep != task["id"]]
    clean_tasks.sort(key=lambda item: item["id"])
    last_task_id = clean_tasks[-1]["id"] if clean_tasks else None
    for task in clean_tasks:
        if task.get("context_scope") not in CONTEXT_SCOPE_LABELS:
            task["context_scope"] = infer_context_scope(task, is_last=task["id"] == last_task_id)

    return {
        "name": _clean_text(workflow.get("name"), DEFAULT_WORKFLOW["name"]),
        "description": _clean_text(workflow.get("description"), DEFAULT_WORKFLOW["description"]),
        "manager_key": manager_key,
        "employees": clean_employees,
        "tasks": clean_tasks,
    }


def ordered_tasks(workflow):
    return list(normalize_workflow(workflow)["tasks"])


def task_map(workflow):
    return {task["id"]: task for task in ordered_tasks(workflow)}


def next_task_id(workflow):
    ids = [int(task.get("id", 0)) for task in (workflow.get("tasks") or []) if str(task.get("id", "")).isdigit()]
    candidate = max(ids or [0]) + 1
    return candidate if candidate > 0 else 1


def parse_deps(value):
    if isinstance(value, list):
        raw = ",".join(str(x) for x in value)
    else:
        raw = str(value or "")
    deps = []
    for item in re.split(r"[,\uFF0C\s]+", raw):
        if not item:
            continue
        try:
            dep = int(item)
        except ValueError:
            continue
        if dep not in deps:
            deps.append(dep)
    return deps


def parse_output_modes(value):
    text = str(value or "").strip().lower()
    if not text:
        return {"text"}
    aliases = {
        "\u6587\u672c": "text",
        "\u6587\u5b57": "text",
        "\u6587\u6848": "text",
        "\u6587\u4ef6": "file",
        "\u9644\u4ef6": "file",
        "\u56fe\u7247": "image",
        "\u56fe\u50cf": "image",
        "\u7167\u7247": "image",
        "\u6d77\u62a5": "image",
        "\u5c01\u9762": "image",
        "\u97f3\u9891": "audio",
        "\u58f0\u97f3": "audio",
        "\u89c6\u9891": "video",
        "\u5f71\u7247": "video",
        "jpeg": "image",
        "jpg": "image",
        "png": "image",
        "webp": "image",
    }
    modes = set()
    for token in re.split(r"[,\uFF0C/\u3001\s|]+", text):
        token = token.strip().lower()
        if not token:
            continue
        modes.add(aliases.get(token, token))
    return modes or {"text"}


def task_requires_image(task):
    return "image" in parse_output_modes((task or {}).get("output_modes", "text"))


def upstream_task_ids(workflow, tid):
    tid = int(tid)
    tasks = task_map(workflow)
    result = []
    seen = set()

    def visit(task_id):
        for dep in tasks.get(task_id, {}).get("deps", []):
            if dep in seen:
                continue
            seen.add(dep)
            visit(dep)
            result.append(dep)

    visit(tid)
    ordered = [task["id"] for task in ordered_tasks(workflow)]
    return [task_id for task_id in ordered if task_id in set(result)]


def previous_task_ids(workflow, tid):
    tid = int(tid)
    ids = []
    for task in ordered_tasks(workflow):
        if task["id"] == tid:
            break
        ids.append(task["id"])
    return ids


def context_task_ids(workflow, task):
    task = task or {}
    tid = int(task.get("id", 0) or 0)
    scope = normalize_context_scope(task.get("context_scope"))
    if scope == CONTEXT_SCOPE_UPSTREAM_DEPS:
        return upstream_task_ids(workflow, tid)
    if scope == CONTEXT_SCOPE_PREVIOUS_TASKS:
        return previous_task_ids(workflow, tid)
    if scope == CONTEXT_SCOPE_ALL_TASKS:
        return [item["id"] for item in ordered_tasks(workflow) if item["id"] != tid]
    return [int(dep) for dep in task.get("deps", []) if int(dep) != tid]


def make_output_package(text="", assets=None, updated_at=None):
    if isinstance(text, dict):
        data = text
        return {
            "text": str(data.get("text") or ""),
            "assets": data.get("assets") if isinstance(data.get("assets"), list) else [],
            "updated_at": data.get("updated_at") or updated_at or time.strftime("%Y-%m-%d %H:%M:%S"),
        }
    return {
        "text": str(text or ""),
        "assets": assets if isinstance(assets, list) else [],
        "updated_at": updated_at or time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def package_text(package):
    if isinstance(package, dict):
        return str(package.get("text") or "")
    if isinstance(package, str):
        return package
    return ""


def package_assets(package):
    if isinstance(package, dict) and isinstance(package.get("assets"), list):
        return package["assets"]
    return []


def package_done(package):
    text = package_text(package).strip()
    assets = package_assets(package)
    if text.startswith("❌"):
        return False
    return bool(text or assets)


def task_done(store, tid):
    try:
        tid = int(tid)
    except (TypeError, ValueError):
        return False
    reviews = (getattr(store, "manager_reviews", {}) or {}).get(str(tid), [])
    if reviews and isinstance(reviews[-1], dict) and reviews[-1].get("passed") is False:
        return False
    return package_done((getattr(store, "outputs", {}) or {}).get(tid))


def is_ready(store, tid):
    task = task_map(getattr(store, "workflow", {})).get(int(tid))
    if not task:
        return False
    return all(task_done(store, dep) for dep in task.get("deps", []))


def downstream_task_ids(workflow, tid):
    tid = int(tid)
    tasks = task_map(workflow)
    result = set()
    frontier = [tid]
    while frontier:
        cur = frontier.pop()
        for task_id, task in tasks.items():
            if cur in task.get("deps", []) and task_id not in result:
                result.add(task_id)
                frontier.append(task_id)
    return sorted(result)


def workflow_dependency_issues(workflow):
    """Return dependency issues that prevent workflow execution.

    ``normalize_workflow`` removes missing and self-referential dependencies.
    This function uses Kahn's algorithm to detect cycles. Forward dependencies
    are valid and are not reported as errors.
    """
    tasks = ordered_tasks(workflow)
    if not tasks:
        return ["The workflow must contain at least one task."]

    task_ids = {task["id"] for task in tasks}
    indegree = {task["id"]: 0 for task in tasks}
    dependents = {task["id"]: [] for task in tasks}
    for task in tasks:
        tid = task["id"]
        for dep in task.get("deps", []):
            if dep not in task_ids:
                continue
            indegree[tid] += 1
            dependents[dep].append(tid)

    ready = sorted(tid for tid, count in indegree.items() if count == 0)
    visited = []
    while ready:
        tid = ready.pop(0)
        visited.append(tid)
        for dependent in sorted(dependents.get(tid, [])):
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                ready.append(dependent)
                ready.sort()

    if len(visited) == len(tasks):
        return []
    blocked = sorted(task_ids - set(visited))
    labels = ", ".join(f"Task {tid}" for tid in blocked)
    return [f"Circular dependency detected: {labels} are waiting on one another. Update these task dependencies."]


def employee_prompt(employee):
    skills = _clean_text(employee.get("skills"), "Understand requirements\nExecute tasks\nSynthesize results")
    return (
        f"You are the {employee.get('name', 'AI Agent')}.\n\n"
        f"[ROLE]\n{employee.get('intro', '')}\n\n"
        f"[CORE SKILLS]\n{skills}\n\n"
        "Complete this assignment according to your role, skills, task instructions, "
        "upstream context, and acceptance criteria."
    )


def asset_summary(assets):
    lines = []
    remaining_extracted_chars = MAX_PROMPT_EXTRACTED_CHARS
    for idx, asset in enumerate(assets or [], start=1):
        if not isinstance(asset, dict):
            continue
        lines.append(
            f"{idx}. {asset.get('name', 'Untitled attachment')} | "
            f"{asset.get('mime', 'application/octet-stream')} | "
            f"{asset.get('size', 0)} bytes"
        )
        extracted = str(asset.get("extracted_text") or "").strip()
        if extracted and remaining_extracted_chars > 0:
            excerpt = extracted[:remaining_extracted_chars]
            lines.append(
                f"[EXTRACTED CONTENT FROM ATTACHMENT {idx} · {asset.get('extraction_format', 'document')}]\n{excerpt}"
            )
            remaining_extracted_chars -= len(excerpt)
            if len(excerpt) < len(extracted):
                lines.append("[The extracted attachment content reached the prompt limit. Remaining content was omitted.]")
        elif asset.get("extraction_status") in {"error", "empty", "unsupported"}:
            lines.append(f"[ATTACHMENT {idx} EXTRACTION NOTE] {asset.get('extraction_note', 'No text was extracted.')}")
    return "\n".join(lines) if lines else "(No attachments)"


def package_to_prompt(package, title="Content"):
    text = package_text(package).strip() or "(No text content)"
    assets = asset_summary(package_assets(package))
    return f"[{title} · TEXT]\n{text}\n\n[{title} · ATTACHMENTS]\n{assets}"
