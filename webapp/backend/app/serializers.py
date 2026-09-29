import json

from .models import Job, JobEvent, Template, User
from .storage import from_data


def user_json(user: User) -> dict:
    return {"id": user.id, "username": user.username}


def template_json(template: Template, analysis_job: Job | None = None) -> dict:
    value = {
        "id": template.id, "name": template.name, "status": template.status,
        "slide_count": template.slide_count, "error": template.error,
        "created_at": template.created_at.isoformat(),
    }
    value["analysis_job"] = None if analysis_job is None else {
        "id": analysis_job.id,
        "status": analysis_job.status,
        "stage": analysis_job.stage,
        "progress": analysis_job.progress,
        "error": analysis_job.error,
        "warnings": [item for item in (analysis_job.warnings or []) if isinstance(item, str)],
        "created_at": analysis_job.created_at.isoformat(),
        "updated_at": analysis_job.updated_at.isoformat(),
    }
    return value


def job_json(job: Job) -> dict:
    stored_warnings = [
        item for item in (job.warnings or [])
        if not (isinstance(item, dict) and item.get("code") == "system_resume")
    ]
    quality_status = "passed"
    result_kind = "primary"
    if job.artifact_path:
        try:
            plan = json.loads(
                (from_data(job.artifact_path) / "plan.json").read_text(encoding="utf-8")
            )
            if plan.get("quality", {}).get("status") == "needs_review":
                quality_status = "needs_review"
            quality = plan.get("quality", {})
            if quality.get("result_kind") in {"primary", "fallback"}:
                result_kind = quality["result_kind"]
            elif any(
                "упрощ" in str(item).casefold() or "fallback" in str(item).casefold()
                for item in quality.get("warnings", [])
            ):
                result_kind = "fallback"
        except (OSError, ValueError, TypeError):
            pass
    value = {
        "id": job.id, "type": job.type, "template_id": job.template_id,
        "template_name": job.template.name if job.template else None,
        "status": job.status, "stage": job.stage, "progress": job.progress,
        "brief_excerpt": job.brief_excerpt, "error": job.error,
        "warnings": [item for item in stored_warnings if isinstance(item, str)],
        "validation_issues": [item for item in stored_warnings if isinstance(item, dict)],
        "quality_status": quality_status,
        "result_kind": result_kind,
        "degraded": result_kind == "fallback",
        "created_at": job.created_at.isoformat(),
        "updated_at": job.updated_at.isoformat(),
    }
    if job.qa_path:
        try:
            value["preview_count"] = len(json.loads((from_data(job.qa_path) / "report.json").read_text(encoding="utf-8")).get("previews", []))
        except (OSError, ValueError):
            value["preview_count"] = 0
    return value


def event_json(event: JobEvent) -> dict:
    return {
        "id": event.id, "stage": event.stage, "progress": event.progress,
        "message": event.message, "created_at": event.created_at.isoformat(),
    }
