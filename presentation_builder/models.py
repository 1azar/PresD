from __future__ import annotations

from pathlib import Path
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BuildIssue(StrictModel):
    level: Literal["error", "warning"]
    code: str
    message: str
    slide_number: int | None = None
    shape_id: int | None = None


class BuildReport(StrictModel):
    status: Literal["ok"] = "ok"
    output: str
    slide_count: int = Field(ge=1)
    qa_dir: str
    previews: list[str] = Field(default_factory=list)
    issues: list[BuildIssue] = Field(default_factory=list)


@runtime_checkable
class ImageGenerator(Protocol):
    def generate(
        self,
        prompt: str,
        *,
        alt_text: str | None,
        slide_number: int,
        shape_id: int,
    ) -> str | Path | bytes: ...


class BuildFailure(RuntimeError):
    def __init__(self, code: str, message: str, issues: list[BuildIssue] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.issues = issues or []

    def as_dict(self) -> dict:
        return {
            "status": "error",
            "code": self.code,
            "message": self.message,
            "issues": [issue.model_dump(mode="json") for issue in self.issues],
        }
