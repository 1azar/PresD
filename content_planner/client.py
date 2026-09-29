from __future__ import annotations

import json
import logging
import random
import time
from typing import Any, Protocol

from llm_config import API_MODES, LLMSettings, STRUCTURED_OUTPUT_MODES, infer_api_mode

logger = logging.getLogger("content_planner.llm")


def _output_tokens(usage: Any) -> int | None:
    if not isinstance(usage, dict):
        return None
    for key in ("output_tokens", "completion_tokens"):
        value = usage.get(key)
        if isinstance(value, int):
            return value
    return None


def _strict_json_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Adapt Pydantic schemas to strict structured-output requirements."""
    value = json.loads(json.dumps(schema))

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            properties = node.get("properties")
            if isinstance(properties, dict):
                # Strict structured output requires every property key to be
                # listed here. Optional values remain optional through their
                # nullable branch (for example chart_asset_id = null).
                node["required"] = list(properties)
                node.setdefault("additionalProperties", False)
            for child in node.values():
                visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)

    visit(value)
    return value


class TextLLM(Protocol):
    model: str

    def complete(
        self,
        prompt: str,
        *,
        json_schema: dict[str, Any] | None = None,
        schema_name: str = "response",
        stage: str = "unknown",
        job_id: str | None = None,
        deadline: float | None = None,
    ) -> str: ...


class ResponseStatusError(RuntimeError):
    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


class ResponseIncompleteError(RuntimeError):
    """The provider stopped generating before it produced a complete response."""


class ResponseContentError(RuntimeError):
    """The provider returned a response without usable text content."""


def _error_category(exc: Exception) -> tuple[str, bool, float | None]:
    if isinstance(exc, ResponseIncompleteError):
        return "incomplete", True, None
    if isinstance(exc, ResponseContentError):
        return "empty_response", True, None
    status = getattr(exc, "status_code", None)
    response = getattr(exc, "response", None)
    if status is None and response is not None:
        status = getattr(response, "status_code", None)
    retry_after: float | None = None
    headers = getattr(response, "headers", None) or getattr(exc, "headers", None)
    if headers:
        try:
            retry_after = float(headers.get("retry-after"))
        except (TypeError, ValueError):
            pass
    name = type(exc).__name__.casefold()
    if status == 429:
        return "rate_limit", True, retry_after
    if isinstance(status, int) and status >= 500:
        return "server", True, retry_after
    if "timeout" in name:
        return "timeout", True, retry_after
    if any(token in name for token in ("connection", "connect", "network")):
        return "connection", True, retry_after
    if isinstance(status, int):
        return f"http_{status}", False, retry_after
    return "client", False, retry_after


class OpenAICompatibleClient:
    def __init__(
        self,
        *,
        client: Any | None = None,
        model: str | None = None,
        retries: int = 1,
        timeout: float | None = None,
        api_mode: str | None = None,
        structured_output: str | None = None,
        settings: LLMSettings | None = None,
    ) -> None:
        if settings is None and client is None:
            settings = LLMSettings.from_env()
        if settings is not None:
            selected_model = model or settings.model
            configured_timeout = timeout if timeout is not None else settings.timeout
            selected_api_mode = api_mode or settings.api_mode
            selected_structured_output = structured_output or settings.structured_output
        else:
            if not model:
                raise ValueError("model must be provided with an injected client")
            selected_model = model
            configured_timeout = timeout if timeout is not None else 90.0
            selected_api_mode = api_mode or infer_api_mode(client)
            selected_structured_output = structured_output or "native"

        self.model = selected_model
        if retries not in {0, 1}:
            raise ValueError("retries must be 0 or 1")
        self.retries = retries
        if not 1 <= configured_timeout <= 300:
            raise ValueError("timeout must be between 1 and 300 seconds")
        self.timeout = configured_timeout
        if selected_api_mode not in API_MODES:
            raise ValueError(f"api_mode must be one of: {', '.join(sorted(API_MODES))}")
        if selected_structured_output not in STRUCTURED_OUTPUT_MODES:
            raise ValueError(
                "structured_output must be one of: "
                f"{', '.join(sorted(STRUCTURED_OUTPUT_MODES))}"
            )
        self.api_mode = selected_api_mode
        self.structured_output = selected_structured_output
        self.settings = settings
        self.client = client or self._create_client()

    def _create_client(self) -> Any:
        from openai import OpenAI

        assert self.settings is not None
        return OpenAI(
            api_key=self.settings.sdk_api_key,
            base_url=self.settings.base_url,
            project=self.settings.project,
            timeout=self.timeout,
            max_retries=0,
        )

    def complete(
        self,
        prompt: str,
        *,
        json_schema: dict[str, Any] | None = None,
        schema_name: str = "response",
        stage: str = "unknown",
        job_id: str | None = None,
        deadline: float | None = None,
    ) -> str:
        last_error: Exception | None = None
        for attempt in range(1, self.retries + 2):
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                raise TimeoutError("planning budget exhausted before LLM request")
            started = time.monotonic()
            response_id: Any = None
            usage: Any = None
            try:
                request_timeout = (
                    min(self.timeout, remaining) if remaining is not None else self.timeout
                )
                prepared_prompt = self._prepare_prompt(prompt, json_schema)
                if self.api_mode == "responses":
                    response = self._create_response(
                        prepared_prompt, json_schema, schema_name, request_timeout,
                    )
                    response_id, usage = self._response_metadata(response)
                    text, response_id, usage = self._parse_response(response)
                else:
                    response = self._create_chat_completion(
                        prepared_prompt, json_schema, schema_name, request_timeout,
                    )
                    response_id, usage = self._response_metadata(response)
                    text, response_id, usage = self._parse_chat_completion(response)
                logger.info(
                    "llm_call job=%s stage=%s attempt=%d latency=%.3f response_id=%s "
                    "output_tokens=%s usage=%s outcome=success error_category=none",
                    job_id or "-", stage, attempt, time.monotonic() - started,
                    response_id, _output_tokens(usage),
                    json.dumps(usage, default=str),
                )
                return text
            except Exception as exc:
                last_error = exc
                category, retryable, retry_after = _error_category(exc)
                logger.warning(
                    "llm_call job=%s stage=%s attempt=%d latency=%.3f response_id=%s "
                    "output_tokens=%s usage=%s outcome=error error_category=%s",
                    job_id or "-", stage, attempt, time.monotonic() - started,
                    response_id or "-", _output_tokens(usage),
                    json.dumps(usage, default=str), category,
                )
                if not retryable or attempt > self.retries:
                    break
                delay = retry_after if retry_after is not None else 0.5 + random.random() * 0.25
                if deadline is not None and time.monotonic() + delay >= deadline:
                    break
                time.sleep(delay)
        assert last_error is not None
        raise last_error

    def _prepare_prompt(
        self, prompt: str, json_schema: dict[str, Any] | None,
    ) -> str:
        if json_schema is None or self.structured_output != "prompt":
            return prompt
        schema = json.dumps(
            _strict_json_schema(json_schema), ensure_ascii=False, separators=(",", ":"),
        )
        return (
            f"{prompt}\n\nReturn only JSON matching this JSON Schema:\n{schema}"
        )

    @staticmethod
    def _response_metadata(response: Any) -> tuple[Any, Any]:
        data = response.model_dump() if hasattr(response, "model_dump") else response
        response_id = getattr(response, "id", None)
        usage = getattr(response, "usage", None)
        if isinstance(data, dict):
            response_id = response_id or data.get("id")
            if usage is None:
                usage = data.get("usage")
        if hasattr(usage, "model_dump"):
            usage = usage.model_dump()
        return response_id, usage

    def _create_response(
        self, prompt: str, json_schema: dict[str, Any] | None,
        schema_name: str, timeout: float,
    ) -> Any:
        arguments: dict[str, Any] = {
            "model": self.model,
            "temperature": 0,
            "input": [{
                "role": "user",
                "content": [{"type": "input_text", "text": prompt}],
            }],
            "timeout": timeout,
        }
        if json_schema is not None and self.structured_output == "native":
            arguments["text"] = {"format": {
                "type": "json_schema",
                "name": schema_name.replace(" ", "_")[:64],
                "schema": _strict_json_schema(json_schema),
                "strict": True,
            }}
        return self.client.responses.create(**arguments)

    def _create_chat_completion(
        self, prompt: str, json_schema: dict[str, Any] | None,
        schema_name: str, timeout: float,
    ) -> Any:
        arguments: dict[str, Any] = {
            "model": self.model,
            "temperature": 0,
            "messages": [{"role": "user", "content": prompt}],
            "timeout": timeout,
        }
        if json_schema is not None and self.structured_output == "native":
            arguments["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": schema_name.replace(" ", "_")[:64],
                    "schema": _strict_json_schema(json_schema),
                    "strict": True,
                },
            }
        return self.client.chat.completions.create(**arguments)

    @classmethod
    def _parse_response(cls, response: Any) -> tuple[str, Any, Any]:
        data = cls._response_data(response)
        response_id = getattr(response, "id", None) or data.get("id")
        usage = getattr(response, "usage", None)
        if usage is None:
            usage = data.get("usage")
        if hasattr(usage, "model_dump"):
            usage = usage.model_dump()
        status = getattr(response, "status", None) or data.get("status")
        if status == "failed":
            error = getattr(response, "error", None) or data.get("error")
            if hasattr(error, "model_dump"):
                error = error.model_dump()
            code = error.get("code") if isinstance(error, dict) else None
            status_code = 429 if code == "rate_limit_exceeded" else (
                500 if code == "server_error" else 400
            )
            message = error.get("message") if isinstance(error, dict) else str(error)
            raise ResponseStatusError(message or "model response failed", status_code)
        if status == "incomplete":
            details = getattr(response, "incomplete_details", None)
            if details is None:
                details = data.get("incomplete_details")
            if hasattr(details, "model_dump"):
                details = details.model_dump()
            reason = details.get("reason") if isinstance(details, dict) else details
            raise ResponseIncompleteError(f"LLM response incomplete (reason={reason!r})")
        text = getattr(response, "output_text", None) or cls._extract_text(response)
        return text, response_id, usage

    @staticmethod
    def _parse_chat_completion(response: Any) -> tuple[str, Any, Any]:
        data = response.model_dump() if hasattr(response, "model_dump") else response
        if not isinstance(data, dict):
            choices = getattr(response, "choices", None)
            data = {
                "id": getattr(response, "id", None),
                "usage": getattr(response, "usage", None),
                "choices": choices,
            }
        choices = data.get("choices") or []
        if not choices:
            raise ResponseContentError("LLM chat completion contains no choices")
        choice = choices[0]
        if hasattr(choice, "model_dump"):
            choice = choice.model_dump()
        if not isinstance(choice, dict):
            choice = {
                "finish_reason": getattr(choice, "finish_reason", None),
                "message": getattr(choice, "message", None),
            }
        finish_reason = choice.get("finish_reason")
        if finish_reason == "length":
            raise ResponseIncompleteError("LLM response incomplete (reason='length')")
        message = choice.get("message")
        if hasattr(message, "model_dump"):
            message = message.model_dump()
        if not isinstance(message, dict):
            message = {"content": getattr(message, "content", None)}
        content = message.get("content")
        if isinstance(content, list):
            content = "\n".join(
                str(item.get("text", "")) for item in content
                if isinstance(item, dict) and item.get("text")
            )
        if not isinstance(content, str) or not content.strip():
            raise ResponseContentError("LLM chat completion contains no text")
        usage = data.get("usage")
        if hasattr(usage, "model_dump"):
            usage = usage.model_dump()
        return content, data.get("id"), usage

    @staticmethod
    def _response_data(response: Any) -> dict[str, Any]:
        if hasattr(response, "model_dump"):
            data = response.model_dump()
        elif isinstance(response, dict):
            data = response
        else:
            fields = (
                "id", "status", "error", "incomplete_details", "usage", "output",
            )
            data = {
                name: getattr(response, name)
                for name in fields if hasattr(response, name)
            }
            if not data and not getattr(response, "output_text", None):
                raise ResponseContentError("LLM response has no inspectable output")
        if not isinstance(data, dict):
            raise ResponseContentError("LLM response payload is not an object")
        return data

    @classmethod
    def _extract_text(cls, response: Any) -> str:
        data = cls._response_data(response)
        texts: list[str] = []
        output = data.get("output") or []
        if not isinstance(output, list):
            output = []
        for item in output:
            if not isinstance(item, dict):
                continue
            content_items = item.get("content") or []
            if not isinstance(content_items, list):
                continue
            for content in content_items:
                if not isinstance(content, dict):
                    continue
                if content.get("type") in {"output_text", "text"} and content.get("text"):
                    texts.append(content["text"])
        if not texts:
            details = data.get("incomplete_details")
            if details:
                reason = details.get("reason") if isinstance(details, dict) else details
                raise ResponseIncompleteError(
                    f"LLM response incomplete (reason={reason!r})"
                )
            raise ResponseContentError(
                "LLM response contains no text "
                f"(status={data.get('status')!r}, error={data.get('error')!r}, "
                f"incomplete={data.get('incomplete_details')!r})"
            )
        return "\n".join(texts)
