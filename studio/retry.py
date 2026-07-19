# -*- coding: utf-8 -*-
"""Retry and network-recovery utilities for model requests.

This module handles request reliability only and contains no domain prompts.
"""

import re
import threading
import time
import urllib.error
import urllib.request


# Up to 15 retries, with approximately 47 minutes of total backoff.
RETRY_DELAYS = (2, 5, 10, 20, 30, 60, 120, 180, 300, 300, 300, 300, 300, 300, 600)
TASK_MAX_RETRIES = len(RETRY_DELAYS)

NETWORK_PROBE_TIMEOUT_SECONDS = 5
NETWORK_PROBE_INTERVAL_SECONDS = 3
MODEL_CALL_CANCEL_POLL_SECONDS = 0.2
MODEL_CALL_HEARTBEAT_SECONDS = 180
PROVIDER_PROBE_URLS = {
    "OpenRouter": "https://openrouter.ai/api/v1/models",
    "Google Gemini": "https://generativelanguage.googleapis.com",
    "OpenAI (GPT)": "https://api.openai.com/v1/models",
    "Anthropic (Claude)": "https://api.anthropic.com",
}


def _is_transient_error(msg):
    """Return whether an error appears transient and worth retrying."""
    text = (msg or "").lower()
    permanent_phrases = (
        "unauthorized", "forbidden", "permission denied",
        "invalid api key", "invalid_api_key", "incorrect api key",
        "bad request", "invalid request", "invalid_request",
        "model not found", "not found", "unsupported parameter",
        "insufficient quota", "insufficient_quota", "billing",
        "content policy", "policy violation", "blocked", "safety",
        "certificate verify failed", "self signed certificate",
        "context length", "maximum context", "request too large",
    )
    if any(phrase in text for phrase in permanent_phrases):
        return False
    if re.search(r"\b(400|401|402|403|404|407|413|422)\b", text):
        return False
    if re.search(r"\b(408|409|425|429|500|502|503|504|520|521|522|523|524|529)\b", text):
        return True
    transient_phrases = (
        "apiconnectionerror", "api connection error",
        "connecterror", "connection error",
        "remoteprotocolerror", "remote protocol", "server disconnected",
        "readerror", "writeerror", "closedresourceerror", "pooltimeout",
        "protocolerror", "protocol error", "localprotocolerror",
        "rate limit", "rate_limit", "ratelimit", "too many requests",
        "timeout", "timed out", "read timed out", "readtimeout", "operation timed out",
        "overload", "overloaded", "temporar", "try again", "again later",
        "connection", "network", "network is unreachable",
        "network changed", "internet connection appears to be offline",
        "name resolution", "temporary failure in name resolution", "nodename nor servname", "dns",
        "econnreset", "connection reset", "connection aborted", "connection refused",
        "connection closed", "connection lost", "connection terminated",
        "remote end closed connection", "broken pipe", "software caused connection abort",
        "sslerror", "ssl error", "ssleoferror", "tls", "eof occurred",
        "curl error 28", "curl error 35", "curl error 52", "curl error 56",
        "service unavailable", "internal server error", "bad gateway", "gateway timeout",
    )
    return any(phrase in text for phrase in transient_phrases)


def _is_connectivity_error(msg):
    """Return whether an error appears to be a local or transport-layer outage."""
    text = (msg or "").lower()
    if any(
        phrase in text
        for phrase in ("rate limit", "rate_limit", "ratelimit", "too many requests", "overload")
    ):
        return False
    if re.search(r"\b(408|520|521|522|523|524)\b", text):
        return True
    connectivity_phrases = (
        "apiconnectionerror", "api connection error",
        "connecterror", "connection error",
        "remoteprotocolerror", "remote protocol", "server disconnected",
        "readerror", "writeerror", "closedresourceerror", "pooltimeout",
        "protocolerror", "protocol error", "localprotocolerror",
        "timeout", "timed out", "read timed out", "readtimeout", "operation timed out",
        "connection", "network", "network is unreachable", "network changed",
        "internet connection appears to be offline",
        "name resolution", "temporary failure in name resolution", "nodename nor servname", "dns",
        "econnreset", "connection reset", "connection aborted", "connection refused",
        "connection closed", "connection lost", "connection terminated",
        "remote end closed connection", "broken pipe", "software caused connection abort",
        "sslerror", "ssl error", "ssleoferror", "tls", "eof occurred",
        "curl error 28", "curl error 35", "curl error 52", "curl error 56",
    )
    return any(phrase in text for phrase in connectivity_phrases)


def _format_retry_delay(seconds):
    if seconds < 60:
        return f"{seconds} seconds"
    minutes, rest = divmod(seconds, 60)
    return f"{minutes} minutes {rest} seconds" if rest else f"{minutes} minutes"


def _log_retry(store, msg):
    if store is not None and hasattr(store, "log_line"):
        store.log_line(msg)


def _sleep_with_cancel(store, seconds):
    if seconds <= 0:
        return not getattr(store, "cancel", False)
    deadline = time.time() + seconds
    while time.time() < deadline:
        if getattr(store, "cancel", False):
            return False
        time.sleep(min(1.0, max(0.0, deadline - time.time())))
    return not getattr(store, "cancel", False)


def _provider_probe_url(service):
    return PROVIDER_PROBE_URLS.get(getattr(service, "provider", ""))


def _probe_provider_endpoint(url):
    if not url:
        return False, "No health-check endpoint is configured"
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "ScriptStudio-Network-Probe/1.0"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=NETWORK_PROBE_TIMEOUT_SECONDS) as response:
            response.read(1)
            return True, f"HTTP {getattr(response, 'status', 'OK')}"
    except urllib.error.HTTPError as exc:
        # Authentication or method errors still prove the network path is reachable.
        return True, f"HTTP {exc.code}"
    except Exception as exc:
        return False, f"{exc.__class__.__name__}: {exc}"


def _wait_for_retry_delay(store, seconds, service=None, task_label=None, probe_early=False):
    if seconds <= 0:
        return not getattr(store, "cancel", False)
    url = _provider_probe_url(service)
    if not (probe_early and url):
        return _sleep_with_cancel(store, seconds)

    label = task_label or "Model request"
    deadline = time.time() + seconds
    next_probe_at = 0.0
    last_detail = ""
    probe_logged = False
    while time.time() < deadline:
        if getattr(store, "cancel", False):
            return False
        now = time.time()
        if now >= next_probe_at:
            ok, detail = _probe_provider_endpoint(url)
            if ok:
                _log_retry(store, f"✅ {label}: the network/API endpoint is reachable again ({detail}). Retrying now.")
                return True
            last_detail = detail
            if not probe_logged:
                _log_retry(store, f"📡 {label}: waiting for the network/API endpoint to recover (latest check: {detail}).")
                probe_logged = True
            next_probe_at = now + NETWORK_PROBE_INTERVAL_SECONDS
        time.sleep(min(1.0, max(0.0, deadline - time.time())))

    if last_detail:
        _log_retry(
            store,
            f"📡 {label}: the wait window ended without confirming endpoint recovery "
            f"(latest check: {last_detail}). Continuing with the scheduled retry.",
        )
    return not getattr(store, "cancel", False)


def _generate_once_cancellable(service, system_prompt, user_prompt, mock_key, attachments, store, label):
    return run_cancellable_call(
        lambda: service.generate(
                system_prompt,
                user_prompt,
                mock_key=mock_key,
                raise_on_error=True,
                attachments=attachments,
        ),
        store,
        label,
    )


def run_cancellable_call(call, store, label):
    """Run a blocking SDK/HTTP call without blocking workflow cancellation."""
    state = {}
    done = threading.Event()

    def invoke():
        try:
            state["result"] = call()
        except Exception as exc:
            state["error"] = exc
        finally:
            done.set()

    started_at = time.time()
    next_heartbeat = MODEL_CALL_HEARTBEAT_SECONDS
    threading.Thread(target=invoke, daemon=True, name=f"blocking-call-{label[:32]}").start()
    while not done.wait(MODEL_CALL_CANCEL_POLL_SECONDS):
        if getattr(store, "cancel", False):
            _log_retry(store, f"⏹ {label}: stopped waiting. Any late API response will be ignored.")
            return False, None
        elapsed = time.time() - started_at
        if elapsed >= next_heartbeat:
            _log_retry(store, f"⏳ {label} is still processing after {int(elapsed)} seconds. You can stop this run at any time.")
            next_heartbeat += MODEL_CALL_HEARTBEAT_SECONDS
    if "error" in state:
        raise state["error"]
    return True, state.get("result")


def _generate_with_retry(
    service,
    system_prompt,
    user_prompt,
    mock_key=None,
    max_retries=TASK_MAX_RETRIES,
    store=None,
    task_label=None,
    attachments=None,
):
    """Call a model with cancellable, long-backoff retries for transient failures."""
    label = task_label or "Model request"
    delays = list(RETRY_DELAYS[:max(0, max_retries)])
    attempts = max_retries + 1
    for attempt in range(attempts):
        if getattr(store, "cancel", False):
            return f"❌ Canceled: {label} received a stop request before the API call."
        try:
            completed, result = _generate_once_cancellable(
                service,
                system_prompt,
                user_prompt,
                mock_key,
                attachments,
                store,
                label,
            )
            if not completed:
                return f"❌ Canceled: {label} stopped waiting for the model response."
            if attempt > 0:
                _log_retry(store, f"✅ {label}: network/API access recovered. Execution resumed after retry {attempt}.")
            return result
        except Exception as exc:
            error = f"{exc.__class__.__name__}: {exc}"
            if _is_transient_error(error) and attempt < attempts - 1:
                delay = delays[attempt] if attempt < len(delays) else delays[-1] if delays else 0
                _log_retry(
                    store,
                    f"🌐 {label}: transient network/API error: {error}. "
                    f"Retry {attempt + 1}/{max_retries} starts in {_format_retry_delay(delay)}.",
                )
                if not _wait_for_retry_delay(
                    store,
                    delay,
                    service=service,
                    task_label=label,
                    probe_early=_is_connectivity_error(error),
                ):
                    return f"❌ Canceled: {label} received a stop request while waiting for retry {attempt + 1}."
                continue
            if _is_transient_error(error):
                _log_retry(store, f"❌ {label}: retries exhausted after a transient network/API error: {error}")
            else:
                _log_retry(store, f"❌ {label}: API request failed with a non-transient error; no extended retry: {error}")
            return f"❌ API request failed after {attempt} retries: {error}"
