from __future__ import annotations

import base64
import json
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from llm_config import API_MODES, LLMSettings, infer_api_mode
from slide_types import SlideClass


class VLMResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    classification: SlideClass
    tags: list[str] = Field(default_factory=list, max_length=10)
    description: str
    confidence: float = Field(ge=0, le=1)

    @field_validator("tags", mode="before")
    @classmethod
    def normalize_tags(cls, value: Any) -> list[str]:
        if not isinstance(value, list):
            return value
        normalized: list[str] = []
        for item in value:
            tag = re.sub(r"[^\w]+", "_", str(item).strip().lower(), flags=re.UNICODE).strip("_")
            if tag and tag not in normalized:
                normalized.append(tag)
        return normalized


class VLMAnalyzer:
    def __init__(
        self,
        client: Any | None = None,
        model: str | None = None,
        prompt_path: str | Path | None = None,
        retries: int = 2,
        timeout: float | None = None,
        api_mode: str | None = None,
        settings: LLMSettings | None = None,
    ) -> None:
        if settings is None and client is None:
            settings = LLMSettings.from_env("VLM", fallback=LLMSettings.from_env())
        if settings is not None:
            self.model = model or settings.model
            self.timeout = timeout if timeout is not None else settings.timeout
            self.api_mode = api_mode or settings.api_mode
        else:
            self.model = model or "test"
            self.timeout = timeout if timeout is not None else 90.0
            self.api_mode = api_mode or infer_api_mode(client)
        if self.api_mode not in API_MODES:
            raise ValueError(f"api_mode must be one of: {', '.join(sorted(API_MODES))}")
        if not 1 <= self.timeout <= 300:
            raise ValueError("timeout must be between 1 and 300 seconds")
        self.settings = settings
        self.prompt_path = (
            Path(prompt_path)
            if prompt_path
            else Path(__file__).parent / "prompts" / "slide_analysis_v1.txt"
        )
        self.prompt = self.prompt_path.read_text(encoding="utf-8")
        self.retries = retries
        self.client = client or self._create_client()

    @staticmethod
    def credentials_available() -> bool:
        try:
            LLMSettings.from_env("VLM", fallback=LLMSettings.from_env())
        except ValueError:
            return False
        return True

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

    def analyze(self, image_path: str | Path, structure: dict[str, Any]) -> VLMResult:
        encoded = base64.b64encode(Path(image_path).read_bytes()).decode("ascii")
        compact = json.dumps(structure, ensure_ascii=False, separators=(",", ":"))
        last_error: Exception | None = None
        for _ in range(self.retries + 1):
            try:
                text = f"{self.prompt}\nSlide structure:\n{compact}"
                image_url = f"data:image/png;base64,{encoded}"
                if self.api_mode == "responses":
                    response = self.client.responses.create(
                        model=self.model,
                        temperature=0,
                        input=[{
                            "role": "user",
                            "content": [
                                {"type": "input_text", "text": text},
                                {"type": "input_image", "image_url": image_url},
                            ],
                        }],
                        timeout=self.timeout,
                    )
                    raw = getattr(response, "output_text", None) or self._extract_text(response)
                else:
                    response = self.client.chat.completions.create(
                        model=self.model,
                        temperature=0,
                        messages=[{
                            "role": "user",
                            "content": [
                                {"type": "text", "text": text},
                                {"type": "image_url", "image_url": {"url": image_url}},
                            ],
                        }],
                        timeout=self.timeout,
                    )
                    raw = self._extract_chat_text(response)
                return VLMResult.model_validate_json(self._clean_json(raw))
            except Exception as exc:
                last_error = exc
        assert last_error is not None
        raise last_error

    @staticmethod
    def _clean_json(value: str) -> str:
        value = value.strip()
        if value.startswith("```"):
            lines = value.splitlines()
            value = "\n".join(lines[1:-1])
        return value.strip()

    @staticmethod
    def _extract_text(response: Any) -> str:
        if hasattr(response, "model_dump"):
            data = response.model_dump()
        elif isinstance(response, dict):
            data = response
        else:
            raise ValueError("VLM response has no output_text")
        texts: list[str] = []
        for item in data.get("output", []):
            for content in item.get("content", []):
                if content.get("type") in {"output_text", "text"} and content.get("text"):
                    texts.append(content["text"])
        if not texts:
            raise ValueError("VLM response contains no text")
        return "\n".join(texts)

    @staticmethod
    def _extract_chat_text(response: Any) -> str:
        data = response.model_dump() if hasattr(response, "model_dump") else response
        if not isinstance(data, dict):
            choices = getattr(response, "choices", None)
            data = {"choices": choices}
        choices = data.get("choices") or []
        if not choices:
            raise ValueError("VLM chat completion contains no choices")
        choice = choices[0]
        if hasattr(choice, "model_dump"):
            choice = choice.model_dump()
        message = (
            choice.get("message")
            if isinstance(choice, dict)
            else getattr(choice, "message", None)
        )
        if hasattr(message, "model_dump"):
            message = message.model_dump()
        content = (
            message.get("content")
            if isinstance(message, dict)
            else getattr(message, "content", None)
        )
        if not isinstance(content, str) or not content.strip():
            raise ValueError("VLM chat completion contains no text")
        return content
