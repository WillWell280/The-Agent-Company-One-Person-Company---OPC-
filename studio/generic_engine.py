# -*- coding: utf-8 -*-
"""Execution engine for configurable AI agent workflows.

The engine is domain-agnostic and understands only:
- workflows: agents, tasks, dependencies, methods, and acceptance criteria;
- content packages: text and attachments;
- manager reviews: structured decisions and revision guidance.
"""

import base64
import io
import json
import math
import random
import re
import socket
import time
import urllib.error
import urllib.request
import uuid

from .generic_workflow import (
    context_scope_label,
    context_task_ids,
    downstream_task_ids,
    employee_prompt,
    is_ready,
    ordered_tasks,
    package_assets,
    package_done,
    package_text,
    package_to_prompt,
    parse_output_modes,
    task_done,
    task_requires_image,
    task_map,
    workflow_dependency_issues,
)
from .llm_service import LLMService
from .retry import TASK_MAX_RETRIES, _generate_with_retry, _sleep_with_cancel, run_cancellable_call
from .web_search import (
    SEARCH_RESULT_COUNT,
    SearchAPIError,
    SearchConfigurationError,
    append_source_list,
    build_search_queries,
    format_search_context,
    normalize_search_provider,
    output_has_inline_source_citation,
    perform_web_search,
    search_api_key,
)


MANAGER_MAX_REVISIONS = 3
IMAGE_HTTP_TIMEOUT_SECONDS = 120
IMAGE_MODEL_TIMEOUT_SECONDS = 600
IMAGE_MODEL_MAX_ATTEMPTS = 2
IMAGE_MODEL_RETRY_SLEEP_SECONDS = 8
MAX_IMAGE_PROMPT_CHARS = 3800
PLACEHOLDER_IMAGE_SHA1 = {
    "63b628babf1db4d953e95585cc1d4197d9ea3555",
}
IMAGE_SIZE_OPTIONS = {
    "square_hd", "square", "portrait_4_3", "portrait_16_9",
    "landscape_4_3", "landscape_16_9",
}
OPENROUTER_IMAGE_ENDPOINT = "https://openrouter.ai/api/v1/images"
OPENAI_IMAGE_GENERATIONS_ENDPOINT = "https://api.openai.com/v1/images/generations"
IMAGE_MODEL_HINTS = (
    "gpt-image",
    "image-1",
    "image-2",
    "image-3",
    "image-mini",
    "seedream",
    "flux",
    "recraft",
    "imagen",
    "dall-e",
    "ideogram",
)
ASSET_HANDOFF_KEYWORDS = (
    "deliver", "delivery", "package", "consolidate", "document", "archive",
    "publish", "final review", "review", "summary", "final",
    "\u4ea4\u4ed8", "\u6253\u5305", "\u6574\u7406", "\u6587\u6863",
    "\u5f52\u6863", "\u53d1\u5e03", "\u7ec8\u5ba1", "\u5ba1\u6838",
    "\u6c47\u603b", "\u6700\u7ec8",
)


def _review_key(tid):
    return str(tid)


def _coerce_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        v = value.strip().lower()
        if v in {"true", "yes", "pass", "passed", "\u901a\u8fc7", "\u5408\u683c"}:
            return True
        if v in {"false", "no", "fail", "failed", "\u4e0d\u901a\u8fc7", "\u4e0d\u5408\u683c"}:
            return False
    return None


def _extract_json_obj(text):
    raw = (text or "").strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        pass
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except Exception:
        return None


def _parse_manager_review(raw):
    data = _extract_json_obj(raw)
    if isinstance(data, dict):
        passed = None
        for key in ("passed", "pass", "qualified", "\u901a\u8fc7", "\u5408\u683c"):
            if key in data:
                passed = _coerce_bool(data.get(key))
                break
        suggestions = data.get(
            "suggestions",
            data.get("\u4fee\u6539\u5efa\u8bae", data.get("\u8fd4\u5de5\u610f\u89c1", "")),
        )
        if isinstance(suggestions, list):
            suggestions = "\n".join(f"- {item}" for item in suggestions)
        return {
            "passed": passed,
            "score": data.get("score", data.get("\u8bc4\u5206")),
            "summary": str(data.get("summary", data.get("\u7ed3\u8bba", "")) or "").strip(),
            "suggestions": str(suggestions or "").strip(),
        }
    text = raw or ""
    low = text.lower()
    if (
        "\u4e0d\u901a\u8fc7" in text
        or "\u4e0d\u5408\u683c" in text
        or "failed" in low
        or re.search(r"\bfail\b", low)
    ):
        passed = False
    elif (
        "\u901a\u8fc7" in text
        or "\u5408\u683c" in text
        or "passed" in low
        or re.search(r"\bpass\b", low)
    ):
        passed = True
    else:
        passed = None
    return {"passed": passed, "score": None, "summary": text[:500], "suggestions": text}


def make_service(store, emp_key):
    cfg = (getattr(store, "emp_configs", {}) or {}).get(emp_key) or {
        "provider": "Mock (Demo)",
        "key": "",
        "model": "mock-studio-model",
    }
    svc = LLMService()
    svc.set_config(
        cfg.get("provider", "Mock (Demo)"),
        cfg.get("key", ""),
        cfg.get("model", "mock-studio-model"),
    )
    return svc


def _image_model_name(svc):
    return str(getattr(svc, "model_name", "") or "").strip()


def _is_image_generation_service(svc):
    provider = str(getattr(svc, "provider", "") or "")
    model = _image_model_name(svc).lower()
    if provider == "OpenRouter" and any(hint in model for hint in IMAGE_MODEL_HINTS):
        return True
    if provider == "OpenAI (GPT)":
        normalized = model.split("/", 1)[-1]
        return normalized.startswith("gpt-image") or normalized.startswith("dall-e")
    return False


def _collect_context_assets(store, task):
    assets = []
    input_package = getattr(store, "input_package", {}) or {}
    assets.extend(package_assets(input_package))
    outputs = getattr(store, "outputs", {}) or {}
    for task_id in _readable_context_task_ids(store, task):
        assets.extend(package_assets(outputs.get(task_id)))
    return assets


def _readable_context_task_ids(store, task):
    workflow = getattr(store, "workflow", {}) or {}
    ids = context_task_ids(workflow, task)
    return [task_id for task_id in ids if task_done(store, task_id)]


def _task_prefers_existing_asset_handoff(task):
    task = task or {}
    text = " ".join(str(task.get(key, "") or "") for key in ("title", "short", "desc", "method"))
    if not text.strip():
        return False
    return any(keyword in text for keyword in ASSET_HANDOFF_KEYWORDS)


def _copy_handoff_asset(asset, source_task_id, source_task_title):
    copied = dict(asset)
    copied["id"] = uuid.uuid4().hex
    copied["source_task_id"] = source_task_id
    copied["source_task_title"] = source_task_title
    copied["source_asset_id"] = asset.get("id")
    return copied


def _collect_context_output_image_assets(store, task):
    workflow = getattr(store, "workflow", {}) or {}
    tasks = task_map(workflow)
    outputs = getattr(store, "outputs", {}) or {}
    result = []
    seen = set()
    for task_id in _readable_context_task_ids(store, task):
        source_task = tasks.get(task_id) or {}
        source_title = source_task.get("title", f"Task {task_id}")
        for asset in package_assets(outputs.get(task_id)):
            if not isinstance(asset, dict):
                continue
            mime = str(asset.get("mime") or "")
            data_b64 = asset.get("data")
            if not mime.startswith("image/") or not data_b64:
                continue
            try:
                data = base64.b64decode(data_b64)
                if _is_placeholder_image(data):
                    continue
            except Exception:
                continue
            dedupe_key = (task_id, asset.get("id") or asset.get("name") or len(result))
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            result.append(_copy_handoff_asset(asset, task_id, source_title))
    return result


def _build_asset_handoff_text(store, task, assets, feedback_notes=None):
    title = task.get("title", f"Task {task.get('id', '')}")
    lines = [
        f"## {title} · Final Asset Delivery",
        "",
        "This is a consolidation, delivery, or packaging task. The system attached the final image assets from upstream tasks instead of generating new images.",
        "",
        "### Delivered Image Assets",
    ]
    for idx, asset in enumerate(assets, start=1):
        source_task_id = asset.get("source_task_id")
        source_title = asset.get("source_task_title") or f"Task {source_task_id}"
        lines.append(
            f"{idx}. {asset.get('name', 'Untitled image')} "
            f"(from Task {source_task_id} · {source_title}; {asset.get('mime', 'image/*')}; {asset.get('size', 0)} bytes)"
        )
    notes = [item for item in (feedback_notes or []) if item]
    if notes:
        lines.extend(["", "### Revision Guidance Applied", "\n\n".join(notes)])
    return "\n".join(lines)


def _has_image_asset(package):
    for asset in package_assets(package):
        if not str(asset.get("mime") or "").startswith("image/"):
            continue
        data_b64 = asset.get("data")
        if data_b64:
            try:
                if _is_placeholder_image(base64.b64decode(data_b64)):
                    continue
            except Exception:
                continue
        return True
    return False


def _image_size_from_modes(task):
    text = str((task or {}).get("output_modes") or "").lower()
    for item in IMAGE_SIZE_OPTIONS:
        if item in text:
            return item
    if any(
        word in text
        for word in (
            "portrait", "poster", "mobile", "vertical",
            "\u7ad6\u7248", "\u7ad6\u56fe", "\u6d77\u62a5", "\u624b\u673a", "\u5c0f\u7ea2\u4e66",
        )
    ):
        return "portrait_4_3"
    if any(word in text for word in ("landscape", "horizontal", "banner", "\u6a2a\u7248", "\u6a2a\u56fe")):
        return "landscape_16_9"
    return "square_hd"


def _image_pixel_size(image_size):
    return {
        "square_hd": "1024x1024",
        "square": "1024x1024",
        "portrait_4_3": "1024x1536",
        "portrait_16_9": "1024x1536",
        "landscape_4_3": "1536x1024",
        "landscape_16_9": "1536x1024",
    }.get(image_size, "1024x1024")


def _image_aspect_ratio(image_size):
    return {
        "square_hd": "1:1",
        "square": "1:1",
        "portrait_4_3": "3:4",
        "portrait_16_9": "9:16",
        "landscape_4_3": "4:3",
        "landscape_16_9": "16:9",
    }.get(image_size, "1:1")


def _trim_prompt(text, limit=MAX_IMAGE_PROMPT_CHARS):
    text = re.sub(r"\s+\n", "\n", str(text or "")).strip()
    if len(text) <= limit:
        return text
    head = text[: int(limit * 0.7)]
    tail = text[-int(limit * 0.2):]
    return f"{head}\n\n...[middle section omitted]...\n\n{tail}"


def _image_filename(task, text):
    raw = str(text or "")
    match = re.search(
        r"([A-Za-z0-9_\-\u4e00-\u9fff()]+"
        r"(?:image|graphic|poster|cover|\u56fe\u7247|\u56fe\u50cf|\u6d77\u62a5|\u5c01\u9762)"
        r"[A-Za-z0-9_\-\u4e00-\u9fff()]*\.(?:png|jpg|jpeg|webp))",
        raw,
        re.IGNORECASE,
    )
    if match:
        name = match.group(1)
        return name.rsplit(".", 1)[0] + ".png"
    title = re.sub(r"[^\w\u4e00-\u9fff\-]+", "_", str(task.get("title") or f"task{task.get('id', '')}")).strip("_")
    return f"{title or 'image_output'}.png"


def _is_placeholder_image(data, final_url=""):
    import hashlib

    if not data:
        return True
    if hashlib.sha1(data).hexdigest() in PLACEHOLDER_IMAGE_SHA1:
        return True
    url = str(final_url or "").lower()
    if "page_image/default" in url or url.endswith("/default.jpeg") or url.endswith("/default.jpg"):
        return True
    return False


def _visual_context_assets(assets):
    out = []
    for asset in assets or []:
        if not isinstance(asset, dict):
            continue
        mime = str(asset.get("mime") or "")
        data_b64 = asset.get("data")
        if not mime.startswith("image/") or not data_b64:
            continue
        try:
            if _is_placeholder_image(base64.b64decode(data_b64)):
                continue
        except Exception:
            continue
        out.append(asset)
    return out


def _visual_reference_block(assets):
    images = _visual_context_assets(assets)
    if not images:
        return ""
    names = ", ".join(str(asset.get("name") or f"Reference image {idx}") for idx, asset in enumerate(images, start=1))
    return (
        "\n\n[VISUAL REFERENCES · REQUIRED]\n"
        f"This request includes {len(images)} visual references: {names}.\n"
        "These are actual image inputs attached to the model request, not just filenames. "
        "Before completing the task, inspect each reference for subject matter, composition, color, style, typography, layout, "
        "brand or character elements, and prohibited deviations. State which visual attributes should be preserved or avoided. "
        "If the task generates an image, the final image specification must explicitly incorporate the relevant reference details."
    )


def _image_input_references(assets, limit=8):
    refs = []
    for asset in _visual_context_assets(assets)[:limit]:
        refs.append({
            "type": "image_url",
            "image_url": {
                "url": f"data:{asset.get('mime', 'image/png')};base64,{asset.get('data')}",
            },
        })
    return refs


def _is_timeout_exception(exc):
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return True
    return "timed out" in str(exc).lower() or "timeout" in exc.__class__.__name__.lower()


def _post_json_once(url, payload, headers=None, timeout=IMAGE_HTTP_TIMEOUT_SECONDS):
    req = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "User-Agent": "GenericAgentWorkbench/1.0",
            **(headers or {}),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        retryable = exc.code in {408, 429, 500, 502, 503, 504}
        return None, f"HTTP {exc.code}: {body[:1200]}", retryable
    except Exception as exc:
        retryable = _is_timeout_exception(exc)
        label = "timeout" if retryable else exc.__class__.__name__
        return None, f"{label}: {exc}", retryable
    try:
        return json.loads(raw.decode("utf-8")), None, False
    except Exception as exc:
        return None, f"The image API returned non-JSON content: {exc}", False


def _post_json(url, payload, headers=None, timeout=IMAGE_HTTP_TIMEOUT_SECONDS,
               attempts=1, retry_label="Image API", store=None):
    attempts = max(1, int(attempts or 1))
    last_error = None
    for attempt in range(1, attempts + 1):
        completed, call_result = run_cancellable_call(
            lambda: _post_json_once(url, payload, headers=headers, timeout=timeout),
            store,
            retry_label,
        )
        if not completed:
            return None, "Canceled while waiting for the image API response."
        response, error, retryable = call_result
        if error is None:
            return response, None
        last_error = error
        if not retryable or attempt >= attempts:
            break
        if store is not None and hasattr(store, "log_line"):
            store.log_line(
                f"⏳ {retry_label}: attempt {attempt} timed out or failed temporarily. "
                f"Retrying in {IMAGE_MODEL_RETRY_SLEEP_SECONDS} seconds (up to {timeout} seconds per attempt)."
            )
        if not _sleep_with_cancel(store, IMAGE_MODEL_RETRY_SLEEP_SECONDS):
            return None, "Image API retry canceled."
    return None, last_error


def _decode_image_payload(item):
    if not isinstance(item, dict):
        return None, "", "The image API returned a non-object value in data[0]."
    mime = str(item.get("media_type") or item.get("mime") or "image/png")
    b64_value = (
        item.get("b64_json")
        or item.get("base64")
        or item.get("data")
        or item.get("image")
    )
    if isinstance(b64_value, str) and b64_value.startswith("data:"):
        match = re.match(r"data:([^;]+);base64,(.+)", b64_value, re.S)
        if not match:
            return None, "", "The image API returned an invalid data URL."
        mime = match.group(1) or mime
        b64_value = match.group(2)
    if isinstance(b64_value, str) and b64_value.strip():
        try:
            return base64.b64decode(b64_value), mime, None
        except Exception as exc:
            return None, "", f"Could not decode the image base64 payload: {exc}"
    image_url = item.get("url")
    if isinstance(image_url, str) and image_url.strip():
        try:
            req = urllib.request.Request(image_url, headers={"User-Agent": "GenericAgentWorkbench/1.0"})
            with urllib.request.urlopen(req, timeout=IMAGE_HTTP_TIMEOUT_SECONDS) as resp:
                data = resp.read()
                mime = resp.headers.get("Content-Type", mime).split(";", 1)[0] or mime
            return data, mime, None
        except Exception as exc:
            return None, "", f"Could not download the image URL: {exc}"
    return None, "", "The image API response did not include b64_json, base64, data, or url."


def _image_asset_from_response(response, task, image_size, generated_by):
    data_list = response.get("data") if isinstance(response, dict) else None
    if not isinstance(data_list, list) or not data_list:
        return None, "The image API response is missing the data array."
    data, mime, error = _decode_image_payload(data_list[0])
    if error:
        return None, error
    if _is_placeholder_image(data):
        return None, "The image API returned a placeholder or empty image."
    ext = "svg" if mime == "image/svg+xml" else (mime.split("/", 1)[1] if "/" in mime else "png")
    if ext == "jpeg":
        ext = "jpg"
    name = _image_filename(task, "")
    name = name.rsplit(".", 1)[0] + f".{ext}"
    return {
        "id": uuid.uuid4().hex,
        "name": name,
        "mime": mime,
        "size": len(data),
        "data": base64.b64encode(data).decode("ascii"),
        "generated_by": generated_by,
        "image_size": image_size,
    }, None


def _generate_image_with_model(store, task, prompt, image_size, context_assets):
    svc = make_service(store, task.get("owner"))
    if not _is_image_generation_service(svc):
        return None, "The assigned agent does not have a dedicated image generation model."
    if not getattr(svc, "api_key", ""):
        return None, "Add an API key for the image generation model."

    provider = str(getattr(svc, "provider", "") or "")
    model = _image_model_name(svc)
    references = _image_input_references(context_assets)
    if provider == "OpenRouter":
        pixel_size = _image_pixel_size(image_size)
        payload = {
            "model": model,
            "prompt": prompt,
            "n": 1,
            "size": pixel_size,
            "quality": "high",
            "output_format": "png",
        }
        if references:
            payload["input_references"] = references
        if store is not None and hasattr(store, "log_line"):
            ref_note = f" with {len(references)} reference images" if references else ""
            store.log_line(f"🎨 Calling the OpenRouter image API with {model} at {pixel_size}{ref_note}.")
        response, error = _post_json(
            OPENROUTER_IMAGE_ENDPOINT,
            payload,
            headers={"Authorization": f"Bearer {svc.api_key}"},
            timeout=IMAGE_MODEL_TIMEOUT_SECONDS,
            attempts=IMAGE_MODEL_MAX_ATTEMPTS,
            retry_label=f"OpenRouter image API · {model}",
            store=store,
        )
        if error:
            return None, f"OpenRouter image request failed: {error}"
        completed, asset_result = run_cancellable_call(
            lambda: _image_asset_from_response(response, task, image_size, f"openrouter:{model}"),
            store,
            f"OpenRouter image download · {model}",
        )
        return asset_result if completed else (None, "Canceled while waiting for the image result.")

    if provider == "OpenAI (GPT)":
        openai_model = model.split("/", 1)[-1]
        payload = {
            "model": openai_model,
            "prompt": prompt,
            "n": 1,
            "size": _image_pixel_size(image_size),
            "quality": "high",
            "response_format": "b64_json",
        }
        if references and store is not None and hasattr(store, "log_line"):
            store.log_line("⚠️ The OpenAI image endpoint is running in text-to-image mode. Reference details are included in the prompt but are not uploaded as image-edit inputs.")
        if store is not None and hasattr(store, "log_line"):
            store.log_line(f"🎨 Calling the OpenAI image generation endpoint with {openai_model}.")
        response, error = _post_json(
            OPENAI_IMAGE_GENERATIONS_ENDPOINT,
            payload,
            headers={"Authorization": f"Bearer {svc.api_key}"},
            timeout=IMAGE_MODEL_TIMEOUT_SECONDS,
            attempts=IMAGE_MODEL_MAX_ATTEMPTS,
            retry_label=f"OpenAI image API · {openai_model}",
            store=store,
        )
        if error:
            return None, f"OpenAI image generation request failed: {error}"
        completed, asset_result = run_cancellable_call(
            lambda: _image_asset_from_response(response, task, image_size, f"openai:{openai_model}"),
            store,
            f"OpenAI image download · {openai_model}",
        )
        return asset_result if completed else (None, "Canceled while waiting for the image result.")

    return None, f"Dedicated image generation is not supported for this provider: {provider}"


def _font(size, bold=False):
    candidates = [
        "/System/Library/Fonts/STHeiti Medium.ttc" if bold else "/System/Library/Fonts/STHeiti Light.ttc",
        "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
        "/System/Library/Fonts/Supplemental/Songti.ttc",
    ]
    try:
        from PIL import ImageFont

        for path in candidates:
            try:
                return ImageFont.truetype(path, size=size)
            except Exception:
                continue
        return ImageFont.load_default()
    except Exception:
        return None


def _canvas_size(image_size):
    return {
        "square_hd": (1536, 1536),
        "square": (1024, 1024),
        "portrait_4_3": (1200, 1600),
        "portrait_16_9": (1080, 1920),
        "landscape_4_3": (1600, 1200),
        "landscape_16_9": (1920, 1080),
    }.get(image_size, (1536, 1536))


def _extract_image_text(store, task, generated_text):
    raw = "\n\n".join([
        str(getattr(store, "input_package", {}).get("text", "") if isinstance(getattr(store, "input_package", {}), dict) else ""),
        str(task.get("desc", "")),
        str(task.get("method", "")),
        str(generated_text or ""),
    ]).replace("\\n", "\n")
    match = re.search(
        r"(?:question|\u95ee\u9898)[:\uFF1A]\s*(.*?)"
        r"(?=(?:➡|swipe for the answer|image must|must not include|additional note|next revision|"
        r"\u53f3\u6ed1\u770b\u7b54\u6848|\u56fe\u7247\u5fc5\u987b|\u4e0d\u5f97\u51fa\u73b0|"
        r"\u53e6\u9700\u6ce8\u610f|\u8bf7\u4e0b\u4e00\u8f6e|\u8fd4\u5de5|$))",
        raw,
        re.DOTALL | re.IGNORECASE,
    )
    if match:
        text = match.group(1).strip()
    else:
        text = raw.strip()
    text = "\n".join(
        line for line in text.splitlines()
        if "swipe for the answer" not in line.lower()
        and "\u53f3\u6ed1\u770b\u7b54\u6848" not in line
        and "➡" not in line
    ).strip()
    text = re.sub(r"\n{3,}", "\n\n", text)
    if len(text) > 900:
        text = text[:900].rstrip() + "..."
    return text or str(task.get("title") or "Image task")


def _wrap_text(draw, text, font, max_width):
    lines = []
    for paragraph in str(text or "").splitlines():
        if paragraph == "":
            lines.append("")
            continue
        current = ""
        for ch in paragraph:
            candidate = current + ch
            bbox = draw.textbbox((0, 0), candidate, font=font)
            if bbox[2] - bbox[0] <= max_width or not current:
                current = candidate
            else:
                lines.append(current)
                current = ch
        if current:
            lines.append(current)
    return lines


def _draw_cat(draw, x, y, scale=1.0):
    orange = "#f59e0b"
    dark = "#4a2a10"
    cream = "#ffe8b8"
    bow = "#3b1f1f"
    s = scale
    # ears
    draw.polygon([(x + 20*s, y + 55*s), (x + 55*s, y + 10*s), (x + 80*s, y + 70*s)], fill=orange, outline=dark)
    draw.polygon([(x + 120*s, y + 70*s), (x + 150*s, y + 10*s), (x + 185*s, y + 55*s)], fill=orange, outline=dark)
    # head
    draw.ellipse((x + 20*s, y + 35*s, x + 185*s, y + 190*s), fill=orange, outline=dark, width=max(2, int(4*s)))
    draw.ellipse((x + 65*s, y + 95*s, x + 140*s, y + 170*s), fill=cream)
    # eyes and nose
    draw.ellipse((x + 65*s, y + 85*s, x + 80*s, y + 102*s), fill=dark)
    draw.ellipse((x + 128*s, y + 85*s, x + 143*s, y + 102*s), fill=dark)
    draw.polygon([(x + 102*s, y + 112*s), (x + 92*s, y + 125*s), (x + 112*s, y + 125*s)], fill="#d97706")
    # whiskers
    for dy in (118, 135):
        draw.line((x + 35*s, y + dy*s, x + 80*s, y + (dy-5)*s), fill=dark, width=max(1, int(2*s)))
        draw.line((x + 125*s, y + (dy-5)*s, x + 175*s, y + dy*s), fill=dark, width=max(1, int(2*s)))
    # bow tie
    draw.polygon([(x + 72*s, y + 185*s), (x + 102*s, y + 168*s), (x + 102*s, y + 202*s)], fill=bow)
    draw.polygon([(x + 132*s, y + 185*s), (x + 102*s, y + 168*s), (x + 102*s, y + 202*s)], fill=bow)
    draw.ellipse((x + 94*s, y + 176*s, x + 110*s, y + 194*s), fill="#111827")


def _fallback_image_asset(store, task, generated_text, image_size):
    try:
        from PIL import Image, ImageDraw, ImageFilter
    except Exception as exc:
        return None, f"Local fallback image rendering is unavailable: {exc}"

    width, height = _canvas_size(image_size)
    rng = random.Random(time.time_ns() ^ hash(str(generated_text or "")))
    bg = Image.new("RGB", (width, height), "#f1dfb8")
    px = bg.load()
    for _ in range(max(8000, width * height // 120)):
        x = rng.randrange(width)
        y = rng.randrange(height)
        delta = rng.randrange(-10, 11)
        r, g, b = px[x, y]
        px[x, y] = (max(0, min(255, r + delta)), max(0, min(255, g + delta)), max(0, min(255, b + delta)))
    bg = bg.filter(ImageFilter.GaussianBlur(radius=0.25))
    draw = ImageDraw.Draw(bg)

    margin = int(width * 0.075)
    radius = int(width * 0.035)
    draw.rounded_rectangle(
        (margin, margin, width - margin, height - margin),
        radius=radius,
        fill="#f7e8c4",
        outline="#8b5e34",
        width=max(4, width // 220),
    )

    title_font = _font(int(width * 0.072), bold=True)
    body_font = _font(int(width * 0.038), bold=False)
    arrow_font = _font(int(width * 0.044), bold=True)
    title = "BRAIN TEASER"
    tb = draw.textbbox((0, 0), title, font=title_font)
    tx = (width - (tb[2] - tb[0])) / 2
    ty = margin + int(height * 0.045)
    for ox, oy, fill in [
        (6, 8, "#6b21a8"),
        (3, 4, "#9333ea"),
        (0, 0, "#c084fc"),
    ]:
        draw.text((tx + ox, ty + oy), title, font=title_font, fill=fill)

    core_text = _extract_image_text(store, task, generated_text)
    max_text_width = width - margin * 2 - int(width * 0.13)
    lines = _wrap_text(draw, core_text, body_font, max_text_width)
    start_y = ty + int(height * 0.16)
    line_h = int(width * 0.055)
    blank_h = int(width * 0.035)
    x = margin + int(width * 0.07)
    y = start_y
    bottom_limit = height - margin - int(height * 0.18)
    for line in lines:
        if y > bottom_limit:
            break
        if line == "":
            y += blank_h
            continue
        draw.text((x, y), line, font=body_font, fill="#2f2417")
        y += line_h

    arrow_text = "SWIPE FOR ANSWER"
    ax = margin + int(width * 0.08)
    ay = height - margin - int(height * 0.105)
    arrow_h = int(width * 0.035)
    arrow_w = int(width * 0.07)
    arrow_y = ay + int(width * 0.018)
    draw.line((ax, arrow_y, ax + arrow_w, arrow_y), fill="#111111", width=max(5, width // 180))
    draw.polygon(
        [
            (ax + arrow_w, arrow_y - arrow_h // 2),
            (ax + arrow_w + arrow_h, arrow_y),
            (ax + arrow_w, arrow_y + arrow_h // 2),
        ],
        fill="#111111",
    )
    text_x = ax + arrow_w + arrow_h + int(width * 0.02)
    draw.text((text_x + 3, ay + 3), arrow_text, font=arrow_font, fill="#7c2d12")
    draw.text((text_x, ay), arrow_text, font=arrow_font, fill="#111111")

    cat_scale = width / 1536
    _draw_cat(draw, width - margin - int(220 * cat_scale), height - margin - int(225 * cat_scale), scale=cat_scale)

    out = io.BytesIO()
    bg.save(out, format="PNG", optimize=True)
    data = out.getvalue()
    return {
        "id": uuid.uuid4().hex,
        "name": _image_filename(task, generated_text),
        "mime": "image/png",
        "size": len(data),
        "data": base64.b64encode(data).decode("ascii"),
        "generated_by": "local_fallback_renderer",
        "image_size": image_size,
    }, None


def _build_image_prompt(store, task, generated_text):
    workflow = getattr(store, "workflow", {}) or {}
    prompt = (
        "SDXL high quality finished image, production-ready design, sharp composition, clean layout, "
        "high resolution, crisp readable typography, no blur, no text truncation, no overlapping text, "
        "accurate Chinese characters if Chinese text is requested.\n\n"
        f"Workflow: {workflow.get('name', 'General Workflow')}\n"
        f"Task title: {task.get('title', '')}\n"
        f"Task description: {task.get('desc', '')}\n"
        f"Task working method and visual requirements:\n{task.get('method', '')}\n\n"
        + package_to_prompt(getattr(store, "input_package", {}) or {}, "User input")
        + "\n\n"
        f"Generated image brief / exact content to render:\n{generated_text}\n\n"
        "Return a complete final image. Do not render UI chrome, watermarks, debug text, placeholder blocks, "
        "or unrelated extra text. Preserve requested line breaks, spacing, colors, characters, background, "
        "logos, icons, arrows, and layout requirements exactly."
    )
    return _trim_prompt(prompt)


def _generate_image_asset(store, task, generated_text, context_assets=None):
    prompt = _build_image_prompt(store, task, generated_text)
    image_size = _image_size_from_modes(task)
    svc = make_service(store, task.get("owner"))
    if _is_image_generation_service(svc):
        return _generate_image_with_model(store, task, prompt, image_size, context_assets or _collect_context_assets(store, task))
    return _fallback_image_asset(store, task, generated_text, image_size)


def _prepare_task_web_search(store, task):
    workflow = getattr(store, "workflow", {}) or {}
    config = getattr(store, "search_config", {}) or {}
    provider = normalize_search_provider(config.get("provider"))
    api_key = search_api_key(config)
    queries = build_search_queries(
        workflow,
        task,
        package_text(getattr(store, "input_package", {}) or {}),
    )
    store.log_line(
        f"🌐 Task {task['id']} started web research · {provider} · "
        f"{len(queries)} queries · Target: {SEARCH_RESULT_COUNT} sources."
    )
    completed, results = run_cancellable_call(
        lambda: perform_web_search(
            provider,
            api_key,
            queries,
            max_results=SEARCH_RESULT_COUNT,
        ),
        store,
        f"Task {task['id']} web research",
    )
    if not completed:
        return None
    retrieved_at = time.strftime("%Y-%m-%d %H:%M:%S")
    store.log_line(
        f"✅ Task {task['id']} web research completed with {len(results)} unique sources. "
        "Later revision rounds will reuse this research snapshot."
    )
    return {
        "provider": provider,
        "queries": queries,
        "results": results,
        "context": format_search_context(
            provider,
            queries,
            results,
            retrieved_at=retrieved_at,
        ),
    }


def _build_task_prompt(store, task, feedback_notes=None, search_context=""):
    workflow = getattr(store, "workflow", {}) or {}
    outputs = getattr(store, "outputs", {}) or {}
    context_assets = _collect_context_assets(store, task)
    context_blocks = []
    context_ids = _readable_context_task_ids(store, task)
    for context_id in context_ids:
        context_task = task_map(workflow).get(context_id)
        context_title = context_task.get("title", f"Task {context_id}") if context_task else f"Task {context_id}"
        context_blocks.append(package_to_prompt(outputs.get(context_id), f"Prior Task {context_id} · {context_title}"))
    context_text = "\n\n".join(context_blocks)
    if not context_text:
        context_text = f"(Current context scope: {context_scope_label(task.get('context_scope'))}. No completed task outputs are available in this scope.)"
    feedback = "\n\n".join([item for item in (feedback_notes or []) if item])
    feedback_block = f"\n\n[PRIOR REVIEW FEEDBACK · ADDRESS EVERY ITEM]\n{feedback}" if feedback else ""
    search_block = f"\n\n{search_context}" if search_context else ""
    return (
        f"[WORKFLOW]\n{workflow.get('name', 'General Workflow')}\n\n"
        f"[WORKFLOW DESCRIPTION]\n{workflow.get('description', '')}\n\n"
        + package_to_prompt(getattr(store, "input_package", {}) or {}, "User Input")
        + _visual_reference_block(context_assets)
        + "\n\n"
        f"[CURRENT TASK]\n"
        f"Task {task['id']}: {task['title']}\n"
        f"Description: {task.get('desc', '')}\n\n"
        f"[WORKING METHOD]\n{task.get('method', '')}\n\n"
        f"[ACCEPTANCE CRITERIA]\n{task.get('acceptance', '')}\n\n"
        f"[EXPECTED OUTPUT MODALITIES]\n{task.get('output_modes', 'text')}\n"
        "If this task includes image output, provide a production-ready visual specification with exact copy, composition, "
        "background, colors, elements, layout, and prohibited elements. The system uses that specification to generate an actual "
        "image attachment; do not merely claim that an image was delivered.\n\n"
        f"[CONTEXT SCOPE]\n{context_scope_label(task.get('context_scope'))}\n\n"
        f"[AVAILABLE PRIOR TASK OUTPUTS]\n{context_text}\n"
        f"{search_block}\n"
        f"{feedback_block}\n\n"
        "Produce the complete deliverable for this task. Do not include greetings or unrelated commentary."
    )


def _build_image_only_spec(store, task, feedback_notes=None, search_context=""):
    return (
        "This task requires an actual image attachment. Use the following requirements as the image generation specification:\n\n"
        + _build_task_prompt(
            store,
            task,
            feedback_notes,
            search_context=search_context,
        )
    )


def _hard_validate(task, package):
    issues = []
    text = package_text(package)
    assets = package_assets(package)
    if not text.strip() and not assets:
        issues.append("The output is empty and contains neither text nor attachments.")
    if text.strip().startswith("❌"):
        issues.append("The output contains an error and cannot be used by downstream tasks.")
    if len(text.strip()) < 30 and not assets:
        issues.append("The text output is too short to demonstrate completion of the task objective.")
    if task_requires_image(task) and not _has_image_asset(package):
        issues.append(
            "This task requires image output, but no image attachment was delivered. "
            "An actual image must be generated and attached before the task can pass."
        )
    if (
        "## Sources (System Generated)" in text
        and not output_has_inline_source_citation(text)
    ):
        issues.append(
            "The web research output does not include an inline [Source N] citation. "
            "Cite at least one valid source immediately after the web-derived claim it supports."
        )
    return len(issues) == 0, "\n".join(issues)


def _manager_employee(store):
    workflow = getattr(store, "workflow", {}) or {}
    employees = workflow.get("employees", {}) or {}
    manager_key = workflow.get("manager_key")
    if manager_key in employees:
        return manager_key, employees[manager_key]
    if employees:
        key = next(iter(employees))
        return key, employees[key]
    return None, None


def _manager_review_prompt(store, task, package, round_no, hard_passed, hard_suggestions):
    hard_block = "Passed" if hard_passed else f"Failed:\n{hard_suggestions}"
    return (
        f"You are reviewing round {round_no} of Task {task['id']} · {task['title']} in a configurable AI agent workflow.\n\n"
        f"[TASK DESCRIPTION]\n{task.get('desc', '')}\n\n"
        f"[WORKING METHOD]\n{task.get('method', '')}\n\n"
        f"[ACCEPTANCE CRITERIA]\n{task.get('acceptance', '')}\n\n"
        f"[PROGRAMMATIC VALIDATION]\n{hard_block}\n\n"
        + package_to_prompt(package, "Output Under Review")
        + "\n\nDetermine whether this output is complete, meets the acceptance criteria, and can reliably support downstream tasks. "
          "Return JSON only. Do not use Markdown:\n"
          "{\n"
          '  "passed": true or false,\n'
          '  "score": 0-100,\n'
          '  "summary": "One-sentence review decision",\n'
          '  "suggestions": "If failed, provide specific revision instructions that can be inserted into the next prompt. If passed, explain why."\n'
          "}"
    )


def _validate_by_manager(store, task, package, round_no, max_retries=TASK_MAX_RETRIES):
    hard_passed, hard_suggestions = _hard_validate(task, package)
    manager_key, manager = _manager_employee(store)
    if not manager_key or not manager:
        return {
            "time": time.strftime("%H:%M:%S"),
            "round": round_no,
            "passed": hard_passed,
            "fatal": False,
            "score": 100 if hard_passed else 0,
            "summary": "No workflow manager is configured. The decision is based on programmatic validation only.",
            "suggestions": hard_suggestions,
            "raw": "",
        }
    svc = make_service(store, manager_key)
    if _is_image_generation_service(svc):
        return {
            "time": time.strftime("%H:%M:%S"),
            "round": round_no,
            "passed": hard_passed,
            "fatal": False,
            "score": 100 if hard_passed else 0,
            "summary": "The workflow manager is configured with an image generation model and cannot perform a text review. The decision is based on programmatic validation only.",
            "suggestions": hard_suggestions,
            "raw": "",
        }
    raw = _generate_with_retry(
        svc,
        employee_prompt(manager),
        _manager_review_prompt(store, task, package, round_no, hard_passed, hard_suggestions),
        mock_key=f"generic_manager_review:{task['id']}",
        max_retries=max_retries,
        store=store,
        task_label=f"Manager review · Task {task['id']}",
        attachments=package_assets(package),
    )
    if isinstance(raw, str) and raw.startswith("❌"):
        return {
            "time": time.strftime("%H:%M:%S"),
            "round": round_no,
            "passed": False,
            "fatal": True,
            "score": 0,
            "summary": "The manager review request failed.",
            "suggestions": raw,
            "raw": raw,
        }

    parsed = _parse_manager_review(raw)
    manager_passed = parsed["passed"] if parsed["passed"] is not None else hard_passed
    suggestions = []
    if not hard_passed and hard_suggestions:
        suggestions.append("[PROGRAMMATIC VALIDATION ISSUES]\n" + hard_suggestions)
    if parsed["suggestions"]:
        suggestions.append(parsed["suggestions"])
    passed = bool(hard_passed and manager_passed)
    return {
        "time": time.strftime("%H:%M:%S"),
        "round": round_no,
        "passed": passed,
        "fatal": False,
        "score": parsed["score"],
        "summary": parsed["summary"] or ("Approved." if passed else "Not approved. Revisions are required."),
        "suggestions": "\n\n".join(suggestions).strip(),
        "raw": raw,
    }


def validate_manual_output(store, tid, package, max_retries=TASK_MAX_RETRIES):
    task = task_map(getattr(store, "workflow", {})).get(int(tid))
    if not task:
        return None
    store.clear_manager_review(_review_key(tid))
    manager_key, _manager = _manager_employee(store)
    with store.lock:
        store.running_employee = manager_key
    store.log_line(f"🧭 Manager review of manually saved output · Task {tid} · Round 1...")
    review = _validate_by_manager(store, task, package, 1, max_retries=max_retries)
    if not getattr(store, "cancel", False):
        store.add_manager_review(_review_key(tid), review)
        if review.get("fatal"):
            with store.lock:
                store.failed_task = tid
        if review.get("passed"):
            store.add_memory(manager_key, f"Reviewed the manually saved output for Task {tid} and approved it.")
        else:
            store.log_line(f"🔁 The manually saved output for Task {tid} did not pass manager review.")
    with store.lock:
        store.running_employee = None
    return review


def run_task(store, tid, max_retries=TASK_MAX_RETRIES):
    workflow = getattr(store, "workflow", {}) or {}
    task = task_map(workflow).get(int(tid))
    if not task:
        return "❌ Task not found."
    employees = workflow.get("employees", {}) or {}
    employee = employees.get(task.get("owner"))
    if not employee:
        return "❌ The assigned agent does not exist. Assign a valid owner in Company Setup."

    review_key = _review_key(tid)
    previous_reviews = list((getattr(store, "manager_reviews", {}) or {}).get(review_key, []))
    previous_failed_review = previous_reviews[-1] if previous_reviews and isinstance(previous_reviews[-1], dict) and previous_reviews[-1].get("passed") is False else None
    feedback_notes = []
    if previous_failed_review:
        advice = previous_failed_review.get("suggestions") or previous_failed_review.get("summary")
        if advice:
            feedback_notes.append("[PRIOR FAILED REVIEW · MUST BE FIXED]\n" + advice)
    if package_done((getattr(store, "outputs", {}) or {}).get(tid)):
        store.log_line(f"♻️ Rerunning Task {tid}. The previous output was cleared to prevent reuse of a failed image.")
    store.clear_output(tid)
    last_package = None
    last_review = None
    search_bundle = None
    search_context = ""
    search_results = []

    if task.get("web_search"):
        with store.lock:
            store.running_employee = task["owner"]
        try:
            search_bundle = _prepare_task_web_search(store, task)
        except (SearchConfigurationError, SearchAPIError) as exc:
            err = f"❌ Web research failed: {exc}"
            store.log_line(f"⛔ Task {tid} · {err[2:]}")
            store.set_output(
                tid,
                {
                    "text": err,
                    "assets": [],
                    "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                },
            )
            with store.lock:
                store.running_employee = None
            return err
        except Exception as exc:
            err = f"❌ Web research error: {exc.__class__.__name__}: {exc}"
            store.log_line(f"⛔ Task {tid} · {err[2:]}")
            store.set_output(
                tid,
                {
                    "text": err,
                    "assets": [],
                    "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                },
            )
            with store.lock:
                store.running_employee = None
            return err
        if search_bundle is None:
            with store.lock:
                store.running_employee = None
            return f"❌ Canceled: Task {tid} stopped waiting for web research results."
        search_context = search_bundle["context"]
        search_results = search_bundle["results"]

    for round_no in range(1, MANAGER_MAX_REVISIONS + 2):
        if getattr(store, "cancel", False):
            return f"❌ Canceled: Task {tid} received a stop request before execution."
        modes = parse_output_modes(task.get("output_modes", "text"))
        image_only = task_requires_image(task) and "text" not in modes
        context_assets = _collect_context_assets(store, task)
        visual_assets = _visual_context_assets(context_assets)
        handoff_assets = _collect_context_output_image_assets(store, task)
        should_handoff_assets = (
            task_requires_image(task)
            and handoff_assets
            and _task_prefers_existing_asset_handoff(task)
        )
        owner_svc = make_service(store, task["owner"])
        owner_is_image_model = _is_image_generation_service(owner_svc)
        with store.lock:
            store.running_employee = task["owner"]
        text_from_model = False
        if should_handoff_assets:
            text = _build_asset_handoff_text(store, task, handoff_assets, feedback_notes=feedback_notes)
            store.log_line(
                f"📦 Task {tid} is an image consolidation/delivery task. Attached "
                f"{len(handoff_assets)} final upstream images without generating replacements."
            )
        elif task_requires_image(task) and owner_is_image_model:
            text = _build_image_only_spec(
                store,
                task,
                feedback_notes,
                search_context=search_context,
            )
            ref_note = "; reference images will be passed to the image API" if visual_assets else ""
            store.log_line(f"🖼️ Task {tid} uses a dedicated image generation model. Skipping chat-completions preprocessing{ref_note}.")
        elif image_only and visual_assets:
            store.log_line(
                f"👁️ Task {tid} includes {len(visual_assets)} reference images. Calling the multimodal model to extract a visual specification first."
            )
            text = _generate_with_retry(
                owner_svc,
                employee_prompt(employee),
                _build_image_only_spec(
                    store,
                    task,
                    feedback_notes,
                    search_context=search_context,
                ),
                mock_key=f"generic_task:{tid}:{task['title']}",
                max_retries=max_retries,
                store=store,
                task_label=f"Task {tid} · {task['title']} · visual reference analysis",
                attachments=context_assets,
            )
            text_from_model = True
        elif image_only:
            text = _build_image_only_spec(
                store,
                task,
                feedback_notes,
                search_context=search_context,
            )
            store.log_line(f"🖼️ Task {tid} is image-only. Skipping text generation and creating the image attachment directly.")
        else:
            text = _generate_with_retry(
                owner_svc,
                employee_prompt(employee),
                _build_task_prompt(
                    store,
                    task,
                    feedback_notes,
                    search_context=search_context,
                ),
                mock_key=f"generic_task:{tid}:{task['title']}",
                max_retries=max_retries,
                store=store,
                task_label=f"Task {tid} · {task['title']}",
                attachments=context_assets,
            )
            text_from_model = True
        if getattr(store, "cancel", False):
            with store.lock:
                store.running_employee = None
            return f"❌ Canceled: Task {tid} received a stop request after the model call returned."
        if isinstance(text, str) and text.startswith("❌"):
            store.set_output(tid, {"text": text, "assets": [], "updated_at": time.strftime("%Y-%m-%d %H:%M:%S")})
            with store.lock:
                store.running_employee = None
            return text
        if search_results:
            if not text_from_model and not output_has_inline_source_citation(text):
                text = (
                    str(text or "").rstrip()
                    + "\n\n## Web Research Validation\n"
                    + "This task used the current web research snapshot to validate context and delivery requirements. [Source 1]"
                )
            text = append_source_list(text, search_results)
        assets = list(handoff_assets) if should_handoff_assets else []
        if task_requires_image(task) and not should_handoff_assets:
            store.log_line(f"🖼️ Task {tid} requires image output. Generating the image attachment...")
            image_asset, image_error = _generate_image_asset(store, task, text, context_assets=context_assets)
            if getattr(store, "cancel", False):
                with store.lock:
                    store.running_employee = None
                return f"❌ Canceled: Task {tid} stopped waiting for image generation."
            if image_error:
                err = f"❌ Image generation failed: {image_error}"
                store.set_output(tid, {"text": err, "assets": [], "updated_at": time.strftime("%Y-%m-%d %H:%M:%S")})
                with store.lock:
                    store.running_employee = None
                return err
            assets.append(image_asset)
            store.log_line(f"✅ Task {tid} image attachment generated: {image_asset['name']}.")
        package = {"text": text, "assets": assets, "updated_at": time.strftime("%Y-%m-%d %H:%M:%S")}
        store.set_output(tid, package)
        last_package = package

        manager_key, _manager = _manager_employee(store)
        with store.lock:
            store.running_employee = manager_key
        store.log_line(f"🧭 Manager review · Task {tid} · Round {round_no}...")
        review = _validate_by_manager(store, task, package, round_no, max_retries=max_retries)
        last_review = review
        if getattr(store, "cancel", False):
            with store.lock:
                store.running_employee = None
            return f"❌ Canceled: Task {tid} received a stop request after the manager review returned."
        store.add_manager_review(review_key, review)

        if review.get("fatal"):
            err = f"❌ Manager review failed: {review.get('suggestions', '')}"
            store.set_output(tid, {"text": err, "assets": [], "updated_at": time.strftime("%Y-%m-%d %H:%M:%S")})
            with store.lock:
                store.running_employee = None
            return err
        if review.get("passed"):
            store.add_memory(task["owner"], f"Completed Task {tid} · {task['title']}.")
            store.add_memory(manager_key, f"Reviewed and approved Task {tid} · {task['title']}.")
            with store.lock:
                store.running_employee = None
            return package

        if round_no <= MANAGER_MAX_REVISIONS:
            advice = review.get("suggestions") or review.get("summary") or "Complete the missing content and align the output more closely with the acceptance criteria."
            feedback_notes.append(advice)
            store.log_line(f"🔁 Task {tid} did not pass manager review. Starting automatic revision {round_no}.")

    advice = (last_review or {}).get("suggestions") or "The output still does not meet the acceptance criteria after multiple revisions."
    err = f"❌ Manager review failed after {MANAGER_MAX_REVISIONS} automatic revisions.\n\n{advice}"
    if last_package and package_done(last_package):
        store.set_output(tid, last_package)
    else:
        store.set_output(tid, {"text": err, "assets": [], "updated_at": time.strftime("%Y-%m-%d %H:%M:%S")})
    with store.lock:
        store.running_employee = None
    return err


def compile_delivery_doc(store):
    workflow = getattr(store, "workflow", {}) or {}
    tasks = ordered_tasks(workflow)
    if not tasks:
        return "# Final Deliverable\n\nThis workflow has no tasks."

    outputs = getattr(store, "outputs", {}) or {}
    parts = [
        f"# {workflow.get('name', 'General Workflow')} · All Task Outputs",
        "",
    ]
    for task in tasks:
        parts.extend([
            f"## Task {task['id']} · {task['title']}",
            "",
            package_text(outputs.get(task["id"])).strip() or "(No text output)",
            "",
        ])
    return "\n".join(parts).strip()


def compile_delivery_assets(store):
    workflow = getattr(store, "workflow", {}) or {}
    tasks = ordered_tasks(workflow)
    tasks_by_id = task_map(workflow)
    outputs = getattr(store, "outputs", {}) or {}
    archived = []
    seen = set()

    for task in tasks:
        task_id = task["id"]
        for index, asset in enumerate(package_assets(outputs.get(task_id))):
            if not isinstance(asset, dict) or not asset.get("data"):
                continue
            source_task_id = asset.get("source_task_id") or task_id
            try:
                source_task_id = int(source_task_id)
            except (TypeError, ValueError):
                source_task_id = task_id
            source_asset_id = asset.get("source_asset_id") or asset.get("id") or f"{task_id}-{index}"
            dedupe_key = (source_task_id, str(source_asset_id))
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            source_task = tasks_by_id.get(source_task_id) or task
            try:
                asset_size = int(asset.get("size") or 0)
            except (TypeError, ValueError):
                asset_size = 0
            archived.append({
                "id": uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"generic-agent-archive:{source_task_id}:{source_asset_id}",
                ).hex,
                "source_asset_id": str(source_asset_id),
                "task_id": source_task_id,
                "task_title": asset.get("source_task_title") or source_task.get("title", f"Task {source_task_id}"),
                "name": asset.get("name") or "Attachment",
                "mime": asset.get("mime") or "application/octet-stream",
                "size": asset_size,
                "data": asset.get("data"),
            })
    return archived


def _all_done(store):
    tasks = ordered_tasks(getattr(store, "workflow", {}) or {})
    return bool(tasks) and all(task_done(store, task["id"]) for task in tasks)


def run_pipeline(store, from_progress=False):
    try:
        with store.lock:
            store.is_running = True
            store.cancel = False
            store.failed_task = None
            store.interrupted_task = None
            store.interrupted_at = ""
        store.save_state()

        workflow = getattr(store, "workflow", {}) or {}
        tasks = ordered_tasks(workflow)
        ok = True
        ran_any = False
        dependency_issues = workflow_dependency_issues(workflow)
        if dependency_issues:
            for issue in dependency_issues:
                store.log_line(f"⚠️ Workflow configuration error: {issue}")
            ok = False

        pending = [task for task in tasks if not task_done(store, task["id"])]
        while ok and pending:
            task = next((item for item in pending if is_ready(store, item["id"])), None)
            if task is None:
                blocked = "; ".join(
                    f"Task {item['id']} (dependencies: {','.join(str(dep) for dep in item.get('deps', [])) or 'none'})"
                    for item in pending
                )
                store.log_line(f"⚠️ No task is ready to run. Blocked tasks: {blocked}.")
                ok = False
                break

            tid = task["id"]
            if store.cancel:
                ok = False
                break
            with store.lock:
                store.running_task = tid
            for downstream_tid in downstream_task_ids(workflow, tid):
                store.clear_output(downstream_tid)
            owner = workflow.get("employees", {}).get(task.get("owner"), {})
            store.log_line(f"▶️ Task {tid} · {task['title']} started · {owner.get('name', task.get('owner'))}")
            ran_any = True
            res = run_task(store, tid)
            if store.cancel:
                ok = False
                break
            if isinstance(res, str) and res.startswith("❌"):
                with store.lock:
                    store.failed_task = tid
                store.log_line(f"❌ Task {tid} failed: {res}")
                ok = False
                break
            store.log_line(f"✅ Task {tid} completed.")
            pending = [item for item in tasks if not task_done(store, item["id"])]

        if ok and not store.cancel and _all_done(store):
            doc = compile_delivery_doc(store)
            assets = compile_delivery_assets(store)
            store.add_doc_history(
                getattr(store, "workflow", {}).get("name", "Workflow Delivery"),
                doc,
                assets=assets,
            )
            store.log_line("🎉 Workflow completed. The final deliverable was archived in Delivery History.")
        elif ok and not store.cancel and not ran_any:
            store.log_line("ℹ️ There are no incomplete tasks ready to run. Check dependencies, input, and task completion status.")
    except Exception as exc:
        store.log_line(f"❌ Workflow execution error: {exc}")
    finally:
        with store.lock:
            store.running_task = None
            store.running_employee = None
            store.is_running = False
        store.save_state()
