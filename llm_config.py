from __future__ import annotations

import os
from dataclasses import dataclass


API_MODES = {"chat_completions", "responses"}
STRUCTURED_OUTPUT_MODES = {"native", "prompt"}


def _env_value(name: str, fallback: str | None = None) -> str | None:
    if name in os.environ:
        value = os.environ[name].strip()
        return value if value or fallback is None else fallback
    return fallback


def _timeout(name: str, fallback: float) -> float:
    raw = _env_value(name)
    if raw in {None, ""}:
        value = fallback
    else:
        try:
            value = float(raw)
        except ValueError as exc:
            raise ValueError(f"{name} must be a number") from exc
    if not 1 <= value <= 300:
        raise ValueError(f"{name} must be between 1 and 300 seconds")
    return value


@dataclass(frozen=True)
class LLMSettings:
    base_url: str
    model: str
    api_key: str = ""
    project: str | None = None
    api_mode: str = "chat_completions"
    structured_output: str = "native"
    timeout: float = 90.0

    @property
    def sdk_api_key(self) -> str:
        return self.api_key or "not-needed"

    @classmethod
    def from_env(
        cls,
        prefix: str = "LLM",
        *,
        fallback: LLMSettings | None = None,
    ) -> LLMSettings:
        base_url = _env_value(
            f"{prefix}_BASE_URL", fallback.base_url if fallback else None,
        )
        model = _env_value(f"{prefix}_MODEL", fallback.model if fallback else None)
        if not base_url:
            raise ValueError(f"{prefix}_BASE_URL must be configured")
        if not model:
            raise ValueError(f"{prefix}_MODEL must be configured")

        api_mode = _env_value(
            f"{prefix}_API_MODE", fallback.api_mode if fallback else "chat_completions",
        ) or "chat_completions"
        if api_mode not in API_MODES:
            raise ValueError(
                f"{prefix}_API_MODE must be one of: {', '.join(sorted(API_MODES))}"
            )

        structured_output = _env_value(
            f"{prefix}_STRUCTURED_OUTPUT",
            fallback.structured_output if fallback else "native",
        ) or "native"
        if structured_output not in STRUCTURED_OUTPUT_MODES:
            raise ValueError(
                f"{prefix}_STRUCTURED_OUTPUT must be one of: "
                f"{', '.join(sorted(STRUCTURED_OUTPUT_MODES))}"
            )

        api_key = _env_value(
            f"{prefix}_API_KEY", fallback.api_key if fallback else "",
        )
        project = _env_value(
            f"{prefix}_PROJECT", fallback.project if fallback else None,
        )
        timeout = _timeout(
            f"{prefix}_TIMEOUT", fallback.timeout if fallback else 90.0,
        )
        return cls(
            base_url=base_url,
            model=model,
            api_key=api_key or "",
            project=project or None,
            api_mode=api_mode,
            structured_output=structured_output,
            timeout=timeout,
        )


def infer_api_mode(client: object) -> str:
    if hasattr(client, "responses"):
        return "responses"
    if hasattr(client, "chat"):
        return "chat_completions"
    raise ValueError("cannot infer API mode from the injected client")
