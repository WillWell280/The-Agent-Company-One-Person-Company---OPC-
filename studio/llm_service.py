# -*- coding: utf-8 -*-
"""Provider catalog, model catalog, and LLM client wrapper for Agent Office."""

import base64
import time
import warnings

# Provider SDKs are optional; call sites return actionable errors when missing.
try:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", FutureWarning)
        import google.generativeai as genai
except ImportError:
    genai = None
try:
    import openai
except ImportError:
    openai = None
try:
    import anthropic
except ImportError:
    anthropic = None
try:
    import httpx
except ImportError:
    httpx = None


# Providers and supported models. Mock mode runs the workflow offline.
MOCK_PROVIDER = "Mock (Demo)"
PROVIDER_LABELS = {
    MOCK_PROVIDER: "None",
}
PROVIDERS = [
    MOCK_PROVIDER,
    "OpenRouter",
    "Google Gemini",
    "OpenAI (GPT)",
    "Anthropic (Claude)",
]
MODELS = {
    MOCK_PROVIDER: ["mock-studio-model"],
    "OpenRouter": [
        "openai/gpt-image-2",
        "openai/gpt-image-1",
        "openai/gpt-5.4-image-2",
        "bytedance-seed/seedream-4.5",
        "anthropic/claude-opus-4.8",
        "anthropic/claude-opus-4.7",
        "anthropic/claude-sonnet-5",
        "anthropic/claude-fable-5",
        "~anthropic/claude-sonnet-latest",
        "google/gemini-3.1-pro-preview",
        "google/gemini-3.5-flash",
        "openai/gpt-5.5-pro",
        "openai/gpt-5.5",
        "deepseek/deepseek-v4-pro",
        "deepseek/deepseek-v4-flash",
    ],
    "Google Gemini": ["gemini-3.5-flash", "gemini-3-flash-preview", "gemini-3-pro-preview", "gemini-3.1-pro-preview"],
    "OpenAI (GPT)": ["gpt-image-2", "gpt-image-1", "gpt-4o", "gpt-4-turbo", "gpt-4o-mini", "gpt-3.5-turbo"],
    "Anthropic (Claude)": ["claude-3-5-sonnet-20240620", "claude-3-opus-20240229", "claude-3-haiku-20240307"],
}

# Maximum output tokens per model call, sized for long-form deliverables.
MAX_TOKENS = 60000

# Timeout for a single live model request. The retry module handles timeouts.
API_TIMEOUT_SECONDS = 600

# Limit connection setup while allowing long reads for large outputs.
API_CONNECT_TIMEOUT_SECONDS = 15
API_WRITE_TIMEOUT_SECONDS = 120
API_POOL_TIMEOUT_SECONDS = 15


def _client_timeout():
    if httpx is None:
        return API_TIMEOUT_SECONDS
    return httpx.Timeout(
        timeout=API_TIMEOUT_SECONDS,
        connect=API_CONNECT_TIMEOUT_SECONDS,
        read=API_TIMEOUT_SECONDS,
        write=API_WRITE_TIMEOUT_SECONDS,
        pool=API_POOL_TIMEOUT_SECONDS,
    )


class LLMService:
    def __init__(self):
        self.provider = MOCK_PROVIDER
        self.api_key = ""
        self.model_name = ""

    def set_config(self, provider, api_key, model_name):
        legacy_mock_provider = "Mock (\u6f14\u793a)"
        self.provider = MOCK_PROVIDER if provider == legacy_mock_provider else provider
        self.api_key = api_key
        self.model_name = model_name

    def generate(self, system_prompt: str, user_prompt: str, mock_key: str = None,
                 raise_on_error: bool = False, attachments=None) -> str:
        # 1. Mock mode
        if self.provider == MOCK_PROVIDER:
            return self._mock_response(user_prompt, mock_key)

        # 2. Live API configuration checks
        if not self.api_key:
            return "❌ Error: Add an API key for this agent in the sidebar, or select None to run in Demo mode."
        if not (self.model_name or "").strip():
            return "❌ Error: Select a model for this agent in the sidebar or enter a custom model ID."

        try:
            if self.provider == "Google Gemini":
                return self._call_gemini(system_prompt, user_prompt, attachments=attachments)
            elif self.provider == "OpenAI (GPT)":
                return self._call_openai(system_prompt, user_prompt, attachments=attachments)
            elif self.provider == "Anthropic (Claude)":
                return self._call_claude(system_prompt, user_prompt, attachments=attachments)
            elif self.provider == "OpenRouter":
                return self._call_openrouter(system_prompt, user_prompt, attachments=attachments)
            else:
                return "❌ Unknown model provider."
        except Exception as e:
            # Re-raise when requested so callers can classify and retry failures.
            if raise_on_error:
                raise
            return f"❌ API request failed: {str(e)}"

    # --- Live provider calls ---
    @staticmethod
    def _image_attachments(attachments):
        out = []
        for asset in attachments or []:
            if not isinstance(asset, dict):
                continue
            mime = str(asset.get("mime") or "")
            data = asset.get("data")
            if mime.startswith("image/") and data:
                out.append({"mime": mime, "data": data, "name": asset.get("name", "image")})
        return out[:8]

    @staticmethod
    def _openai_user_content(user_prompt, attachments):
        images = LLMService._image_attachments(attachments)
        if not images:
            return user_prompt
        content = [{"type": "text", "text": user_prompt}]
        for image in images:
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:{image['mime']};base64,{image['data']}"},
            })
        return content

    @staticmethod
    def _claude_user_content(user_prompt, attachments):
        images = LLMService._image_attachments(attachments)
        if not images:
            return [{"role": "user", "content": user_prompt}]
        content = [{"type": "text", "text": user_prompt}]
        for image in images:
            content.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": image["mime"],
                    "data": image["data"],
                },
            })
        return [{"role": "user", "content": content}]

    def _call_gemini(self, system_prompt, user_prompt, attachments=None):
        if not genai:
            return "❌ Missing dependency. Run: pip install google-generativeai"
        genai.configure(api_key=self.api_key)
        model = genai.GenerativeModel(model_name=self.model_name, system_instruction=system_prompt)
        parts = [user_prompt]
        for image in self._image_attachments(attachments):
            try:
                parts.append({
                    "mime_type": image["mime"],
                    "data": base64.b64decode(image["data"]),
                })
            except Exception:
                continue
        response = model.generate_content(
            parts,
            generation_config={"max_output_tokens": MAX_TOKENS},
            request_options={"timeout": API_TIMEOUT_SECONDS},
        )
        return response.text

    def _call_openai(self, system_prompt, user_prompt, attachments=None):
        if not openai:
            return "❌ Missing dependency. Run: pip install openai"
        client = openai.OpenAI(api_key=self.api_key, timeout=_client_timeout())
        response = client.chat.completions.create(
            model=self.model_name,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": self._openai_user_content(user_prompt, attachments)},
            ],
            temperature=0.7,
            max_tokens=MAX_TOKENS,
        )
        return response.choices[0].message.content

    def _call_claude(self, system_prompt, user_prompt, attachments=None):
        if not anthropic:
            return "❌ Missing dependency. Run: pip install anthropic"
        client = anthropic.Anthropic(api_key=self.api_key, timeout=_client_timeout())
        response = client.messages.create(
            model=self.model_name,
            max_tokens=MAX_TOKENS,
            system=system_prompt,
            messages=self._claude_user_content(user_prompt, attachments),
        )
        return response.content[0].text

    def _call_openrouter(self, system_prompt, user_prompt, attachments=None):
        if not openai:
            return "❌ Missing dependency. Run: pip install openai"
        client = openai.OpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=self.api_key,
            timeout=_client_timeout(),
        )
        response = client.chat.completions.create(
            model=self.model_name,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": self._openai_user_content(user_prompt, attachments)},
            ],
            max_tokens=MAX_TOKENS,
            extra_body={"reasoning": {"enabled": True}},
        )
        return response.choices[0].message.content

    # --- Workflow demo responses, routed by mock_key ---
    def _mock_response(self, prompt_content: str, mock_key: str = None) -> str:
        time.sleep(0.6)
        if mock_key:
            if mock_key.startswith("generic_manager_review:"):
                return (
                    '{"passed": true, "score": 95, '
                    '"summary": "Approved. The deliverable is complete and meets the task requirements.", '
                    '"suggestions": "Approved. This task is ready for the next stage."}'
                )
            if mock_key.startswith("generic_task:"):
                parts = mock_key.split(":", 2)
                title = parts[2] if len(parts) >= 3 else "General Task"
                response = (
                    f"# {title}\n\n"
                    "## Objective\n"
                    "This result reflects the current workflow, user input, upstream outputs, and task acceptance criteria.\n\n"
                    "## Core Deliverable\n"
                    "This is a general-purpose result generated in Demo mode. With a live model, this section is replaced by a production deliverable created by the assigned agent according to its role and skills.\n\n"
                    "## Multimodal Support\n"
                    "If this task requires image or file deliverables, you can attach them in the task output area. Image attachments are passed to vision-capable models as multimodal context for downstream tasks.\n\n"
                    "## Next Step\n"
                    "The workflow manager should review this stage against its acceptance criteria before downstream work begins."
                )
                if "[WEB RESEARCH · RETRIEVED:" in prompt_content:
                    response += (
                        "\n\n## Web Research Validation\n"
                        "This demo result used the current web research snapshot and cited the relevant evidence by source number. [Source 1]"
                    )
                return response
        return "(Demo mode) Unrecognized task type."
