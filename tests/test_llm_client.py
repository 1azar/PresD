from __future__ import annotations

from types import SimpleNamespace

import pytest

from content_planner.client import (
    OpenAICompatibleClient,
    ResponseContentError,
    ResponseIncompleteError,
    _strict_json_schema,
)
from llm_config import LLMSettings
from template_model.vlm import VLMAnalyzer


class Responses:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class ChatCompletions(Responses):
    pass


def chat_client(outcomes):
    completions = ChatCompletions(outcomes)
    return SimpleNamespace(chat=SimpleNamespace(completions=completions)), completions


class HTTPFailure(RuntimeError):
    def __init__(self, status_code: int):
        super().__init__(str(status_code))
        self.status_code = status_code
        self.response = SimpleNamespace(status_code=status_code, headers={"retry-after": "0"})


def response(text: str = '{}', usage=None):
    return SimpleNamespace(output_text=text, id="response-1", usage=usage, status="completed")


def chat_response(text: str = '{}', finish_reason: str = "stop", usage=None):
    return {
        "id": "chat-1",
        "usage": usage,
        "choices": [{
            "finish_reason": finish_reason,
            "message": {"content": text},
        }],
    }


def test_uses_native_json_schema_and_disables_sdk_style_extra_retries():
    responses = Responses([response()])
    client = OpenAICompatibleClient(client=SimpleNamespace(responses=responses), model="test")
    assert client.complete("prompt", json_schema={"type": "object"}, schema_name="draft") == "{}"
    assert responses.calls[0]["text"]["format"] == {
        "type": "json_schema", "name": "draft",
        "schema": {"type": "object"}, "strict": True,
    }
    assert responses.calls[0]["timeout"] == 90.0


def test_strict_schema_requires_nullable_optional_keys():
    schema = _strict_json_schema({
        "type": "object",
        "properties": {
            "purpose": {"type": "string"},
            "chart_asset_id": {
                "anyOf": [{"type": "string"}, {"type": "null"}],
            },
        },
    })

    assert schema["required"] == ["purpose", "chart_asset_id"]
    assert {branch["type"] for branch in schema["properties"]["chart_asset_id"]["anyOf"]} == {
        "string", "null",
    }


def test_timeout_can_be_configured_from_environment(monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", "http://localhost:11434/v1")
    monkeypatch.setenv("LLM_MODEL", "local-model")
    monkeypatch.setenv("LLM_TIMEOUT", "75")
    responses = Responses([response()])
    client = OpenAICompatibleClient(
        client=SimpleNamespace(responses=responses),
        settings=LLMSettings.from_env(),
        api_mode="responses",
    )
    assert client.complete("prompt") == "{}"
    assert responses.calls[0]["timeout"] == 75.0


@pytest.mark.parametrize("status", [429, 500, 503])
def test_retries_rate_limit_and_server_errors_once(status):
    responses = Responses([HTTPFailure(status), response()])
    client = OpenAICompatibleClient(client=SimpleNamespace(responses=responses), model="test")
    assert client.complete("prompt") == "{}"
    assert len(responses.calls) == 2


def test_does_not_retry_schema_or_other_client_errors():
    responses = Responses([HTTPFailure(400), response()])
    client = OpenAICompatibleClient(client=SimpleNamespace(responses=responses), model="test")
    with pytest.raises(HTTPFailure):
        client.complete("prompt")
    assert len(responses.calls) == 1


def test_retries_rate_limit_reported_inside_failed_response():
    failed = SimpleNamespace(
        output_text="", id="response-failed", usage=None, status="failed",
        error={"code": "rate_limit_exceeded", "message": "quota exceeded"},
    )
    responses = Responses([failed, response()])
    client = OpenAICompatibleClient(client=SimpleNamespace(responses=responses), model="test")
    assert client.complete("prompt") == "{}"
    assert len(responses.calls) == 2


def test_does_not_start_request_after_budget_is_exhausted():
    responses = Responses([response()])
    client = OpenAICompatibleClient(client=SimpleNamespace(responses=responses), model="test")
    with pytest.raises(TimeoutError):
        client.complete("prompt", deadline=0)
    assert responses.calls == []


def test_logs_stage_and_output_token_count(caplog):
    responses = Responses([response(usage={"output_tokens": 17})])
    client = OpenAICompatibleClient(client=SimpleNamespace(responses=responses), model="test")

    with caplog.at_level("INFO", logger="content_planner.llm"):
        client.complete("prompt", stage="content analysis", job_id="job-1")

    assert "stage=content analysis" in caplog.text
    assert "output_tokens=17" in caplog.text


def test_extract_text_skips_null_content_entries():
    value = SimpleNamespace(
        output_text="", id="response-mixed", usage=None, status="completed",
        output=[
            {"type": "reasoning", "content": None},
            {"type": "message", "content": [
                {"type": "output_text", "text": '{"ok":true}'},
            ]},
        ],
    )
    responses = Responses([value])
    client = OpenAICompatibleClient(
        client=SimpleNamespace(responses=responses), model="test", retries=0,
    )

    assert client.complete("prompt") == '{"ok":true}'


def test_retries_incomplete_response_then_returns_text(monkeypatch, caplog):
    incomplete = SimpleNamespace(
        output_text="", id="response-incomplete", usage={"output_tokens": 32768},
        status="incomplete", incomplete_details={"reason": "max_output_tokens"},
        output=[{"type": "message", "content": None}],
    )
    responses = Responses([incomplete, response('{"ok":true}')])
    client = OpenAICompatibleClient(client=SimpleNamespace(responses=responses), model="test")
    monkeypatch.setattr("content_planner.client.time.sleep", lambda _: None)

    with caplog.at_level("INFO", logger="content_planner.llm"):
        result = client.complete("prompt", stage="catalog slide reduction")

    assert result == '{"ok":true}'
    assert len(responses.calls) == 2
    assert "stage=catalog slide reduction" in caplog.text
    assert "response_id=response-incomplete" in caplog.text
    assert "output_tokens=32768" in caplog.text
    assert "error_category=incomplete" in caplog.text


def test_repeated_incomplete_response_has_actionable_error(monkeypatch):
    incomplete = SimpleNamespace(
        output_text="", id="response-incomplete", usage={"output_tokens": 32768},
        status="incomplete", incomplete_details={"reason": "max_output_tokens"},
        output=[{"type": "message", "content": None}],
    )
    responses = Responses([incomplete, incomplete])
    client = OpenAICompatibleClient(client=SimpleNamespace(responses=responses), model="test")
    monkeypatch.setattr("content_planner.client.time.sleep", lambda _: None)

    with pytest.raises(ResponseIncompleteError, match="max_output_tokens"):
        client.complete("prompt")

    assert len(responses.calls) == 2


def test_empty_response_raises_content_error_instead_of_type_error():
    with pytest.raises(ResponseContentError, match="contains no text"):
        OpenAICompatibleClient._extract_text({
            "status": "completed",
            "output": [{"type": "message", "content": None}],
        })


def test_chat_completions_uses_native_json_schema():
    injected, completions = chat_client([chat_response('{"ok":true}')])
    client = OpenAICompatibleClient(client=injected, model="local-model")

    result = client.complete(
        "prompt", json_schema={"type": "object"}, schema_name="local result",
    )

    assert result == '{"ok":true}'
    assert completions.calls[0]["messages"] == [{"role": "user", "content": "prompt"}]
    assert completions.calls[0]["response_format"] == {
        "type": "json_schema",
        "json_schema": {
            "name": "local_result",
            "schema": {"type": "object"},
            "strict": True,
        },
    }


def test_prompt_structured_output_omits_provider_specific_format():
    injected, completions = chat_client([chat_response()])
    client = OpenAICompatibleClient(
        client=injected, model="local-model", structured_output="prompt",
    )

    client.complete("prompt", json_schema={"type": "object"})

    call = completions.calls[0]
    assert "response_format" not in call
    assert "Return only JSON matching this JSON Schema" in call["messages"][0]["content"]


def test_chat_length_response_is_retried(monkeypatch):
    injected, completions = chat_client([
        chat_response("", finish_reason="length"),
        chat_response('{"ok":true}'),
    ])
    client = OpenAICompatibleClient(client=injected, model="local-model")
    monkeypatch.setattr("content_planner.client.time.sleep", lambda _: None)

    assert client.complete("prompt") == '{"ok":true}'
    assert len(completions.calls) == 2


def test_settings_allow_local_endpoint_without_api_key(monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", "http://localhost:11434/v1")
    monkeypatch.setenv("LLM_MODEL", "local-model")
    monkeypatch.delenv("LLM_API_KEY", raising=False)

    settings = LLMSettings.from_env()

    assert settings.api_key == ""
    assert settings.sdk_api_key == "not-needed"
    assert settings.api_mode == "chat_completions"


def test_vlm_settings_override_individual_llm_values(monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", "https://text.example/v1")
    monkeypatch.setenv("LLM_MODEL", "text-model")
    monkeypatch.setenv("LLM_API_KEY", "text-key")
    monkeypatch.setenv("VLM_MODEL", "vision-model")

    llm = LLMSettings.from_env()
    vlm = LLMSettings.from_env("VLM", fallback=llm)

    assert vlm.base_url == "https://text.example/v1"
    assert vlm.model == "vision-model"
    assert vlm.api_key == "text-key"


def test_invalid_api_mode_has_actionable_error(monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", "http://localhost:11434/v1")
    monkeypatch.setenv("LLM_MODEL", "local-model")
    monkeypatch.setenv("LLM_API_MODE", "native-provider")

    with pytest.raises(ValueError, match="LLM_API_MODE"):
        LLMSettings.from_env()


def test_vlm_is_available_for_local_endpoint_without_api_key(monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", "http://localhost:11434/v1")
    monkeypatch.setenv("LLM_MODEL", "vision-model")
    monkeypatch.delenv("LLM_API_KEY", raising=False)

    assert VLMAnalyzer.credentials_available()
