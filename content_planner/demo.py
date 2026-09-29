from __future__ import annotations

from pathlib import Path

from .models import PlanningRequest


DEMO_REQUEST_PATH = Path(__file__).parent / "examples" / "it_team_request.json"


def demo_request() -> PlanningRequest:
    return PlanningRequest.model_validate_json(DEMO_REQUEST_PATH.read_text(encoding="utf-8"))
