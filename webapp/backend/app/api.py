from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import secrets
import shutil
import subprocess
from pathlib import Path

from fastapi import APIRouter, Cookie, Depends, File, Form, Header, Request, Response, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field
from redis import Redis
from redis.exceptions import RedisError
from rq.command import send_stop_job_command
from rq.exceptions import NoSuchJobError
from rq.job import Job as RQJob
from sqlalchemy import and_, delete, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DBSession

from content_planner.structured_assets import StructuredAssetError, parse_json_assets
from template_model.analyzer import render_pdf

from .config import settings
from .database import get_db
from .errors import APIError
from .extractors import ContentError, IMAGE_EXTENSIONS, validate_content_file
from .models import Job, JobEvent, JobFile, Session, Template, User, now
from .queue import queue
from .security import COOKIE_NAME, create_session, current_user, hash_password, verify_password
from .serializers import event_json, job_json, template_json, user_json
from .storage import from_data, read_limited, relative_to_data, user_root, validate_pptx
from .tasks import analyze_template, generate_presentation
from .tasks import _atomic_write_text


router = APIRouter(prefix="/api/v1")
logger = logging.getLogger("webapp.api")
USERNAME = re.compile(r"^[\w.@+-]{3,80}$", re.UNICODE)


class Credentials(BaseModel):
    username: str = Field(min_length=3, max_length=80)
    password: str = Field(min_length=8, max_length=200)


class TemplateAnalysisSettings(BaseModel):
    use_vlm: bool = True
    catalog_workers: int = Field(default=4, ge=1, le=8)
    enrichment_workers: int = Field(
        default=settings.template_enrichment_workers, ge=1, le=8,
    )


def _set_cookie(response: Response, raw: str) -> None:
    response.set_cookie(
        COOKIE_NAME, raw, max_age=settings.session_days * 86400, httponly=True,
        secure=settings.cookie_secure, samesite="lax", path="/",
    )


@router.post("/auth/login")
def login(payload: Credentials, response: Response, db: DBSession = Depends(get_db)):
    username = payload.username.strip()
    configured_password = settings.allowed_clients.get(username)
    if configured_password is None or not secrets.compare_digest(configured_password, payload.password):
        raise APIError(401, "invalid_credentials", "Неверный логин или пароль")
    if not USERNAME.fullmatch(username):
        raise APIError(401, "invalid_credentials", "Неверный логин или пароль")
    user = db.scalar(select(User).where(User.username == username))
    if not user:
        user = User(username=username, password_hash=hash_password(payload.password))
        db.add(user)
        db.flush()
    elif not verify_password(user.password_hash, payload.password):
        db.execute(delete(Session).where(Session.user_id == user.id))
        user.password_hash = hash_password(payload.password)
    raw = create_session(db, user)
    _set_cookie(response, raw)
    return user_json(user)


@router.post("/auth/logout", status_code=204)
def logout(response: Response, raw: str | None = Cookie(default=None, alias=COOKIE_NAME), db: DBSession = Depends(get_db)):
    if raw:
        db.execute(delete(Session).where(Session.id == hashlib.sha256(raw.encode()).hexdigest()))
        db.commit()
    response.delete_cookie(COOKIE_NAME, path="/")


@router.get("/auth/me")
def me(user: User = Depends(current_user)):
    return user_json(user)


def _owned_template(db: DBSession, user: User, template_id: str) -> Template:
    value = db.scalar(select(Template).where(
        Template.id == template_id,
        Template.owner_id == user.id,
        Template.status != "deleted",
    ))
    if not value:
        raise APIError(404, "not_found", "Шаблон не найден")
    return value


def _owned_template_for_update(db: DBSession, user: User, template_id: str) -> Template:
    value = db.scalar(select(Template).where(
        Template.id == template_id,
        Template.owner_id == user.id,
        Template.status != "deleted",
    ).with_for_update())
    if not value:
        raise APIError(404, "not_found", "Шаблон не найден")
    return value


def _owned_job(db: DBSession, user: User, job_id: str) -> Job:
    value = db.scalar(select(Job).where(Job.id == job_id, Job.owner_id == user.id))
    if not value:
        raise APIError(404, "not_found", "Задача не найдена")
    return value


def _owned_job_for_update(db: DBSession, user: User, job_id: str) -> Job:
    value = db.scalar(
        select(Job).where(Job.id == job_id, Job.owner_id == user.id).with_for_update()
    )
    if not value:
        raise APIError(404, "not_found", "Задача не найдена")
    return value


def _stop_rq_generation(job_id: str) -> None:
    """Best-effort removal/stop; database state remains the source of truth."""
    connection = Redis.from_url(settings.redis_url)
    rq_id = f"generation-{job_id}"
    try:
        rq_job = RQJob.fetch(rq_id, connection=connection)
    except NoSuchJobError:
        return
    raw_status = rq_job.get_status(refresh=True)
    status = getattr(raw_status, "value", raw_status)
    if status == "started":
        send_stop_job_command(connection, rq_id)
    elif status in {"queued", "deferred", "scheduled"}:
        rq_job.cancel()


def _latest_analysis_job(db: DBSession, template_id: str) -> Job | None:
    return db.scalar(
        select(Job).where(
            Job.template_id == template_id,
            Job.type == "template_analysis",
        ).order_by(Job.created_at.desc(), Job.id.desc())
    )


def _prepare_analysis_request(
    job: Job, template: Template, root: Path, *, use_vlm: bool,
    catalog_workers: int, enrichment_workers: int, preserve_existing: bool,
) -> None:
    artifact = root / "analyses" / job.id
    artifact.mkdir(parents=True, exist_ok=True)
    base_path = root / f"base-{job.id}"
    request_data = {
        "template_id": template.id,
        "source_path": relative_to_data(root / "source.pptx"),
        "base_model_path": relative_to_data(base_path),
        "previous_model_path": template.model_path,
        "use_vlm": use_vlm,
        "catalog_workers": catalog_workers,
        "enrichment_workers": enrichment_workers,
        "vlm_timeout": settings.template_vlm_timeout,
        "preserve_existing": preserve_existing,
        "system_resume_count": 0,
    }
    _atomic_write_text(
        artifact / "request.json",
        json.dumps(request_data, ensure_ascii=False, indent=2) + "\n",
    )
    job.artifact_path = relative_to_data(artifact)


@router.get("/templates")
def templates(user: User = Depends(current_user), db: DBSession = Depends(get_db)):
    values = db.scalars(select(Template).where(
        Template.owner_id == user.id,
        Template.status != "deleted",
    ).order_by(Template.created_at.desc())).all()
    return {"items": [template_json(value, _latest_analysis_job(db, value.id)) for value in values]}


@router.post("/templates", status_code=202)
async def upload_template(
    file: UploadFile = File(...), use_vlm: bool = Form(True), catalog_workers: int = Form(4, ge=1, le=8),
    enrichment_workers: int = Form(settings.template_enrichment_workers, ge=1, le=8),
    user: User = Depends(current_user), db: DBSession = Depends(get_db),
):
    if Path(file.filename or "").suffix.lower() != ".pptx":
        raise APIError(422, "invalid_template_type", "Шаблон должен иметь расширение .pptx")
    data = await read_limited(file, settings.max_template_bytes)
    digest = validate_pptx(data)
    existing = db.scalar(select(Template).where(
        Template.owner_id == user.id, Template.sha256 == digest,
    ).with_for_update())
    if existing and existing.status != "deleted":
        return template_json(existing, _latest_analysis_job(db, existing.id))
    name = Path(file.filename or "template.pptx").name[:255]
    if existing:
        template = existing
        template.name = name
        template.status = "processing"
        template.slide_count = None
        template.model_path = None
        template.error = None
        template.created_at = now()
        template.updated_at = template.created_at
    else:
        template = Template(owner_id=user.id, name=name, sha256=digest)
        db.add(template)
        db.flush()
    root = user_root(user.id) / "templates" / template.id
    # A previous interrupted deletion must not leak stale models into a
    # reactivated template.
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    source = root / "source.pptx"
    source.write_bytes(data)
    job = Job(owner_id=user.id, template_id=template.id, type="template_analysis", brief_excerpt=template.name)
    db.add(job)
    db.flush()
    _prepare_analysis_request(
        job, template, root, use_vlm=use_vlm,
        catalog_workers=catalog_workers, enrichment_workers=enrichment_workers,
        preserve_existing=False,
    )
    # Keep the legacy non-null contract while the template remains unavailable
    # until this immutable version has actually been built.
    template.model_path = relative_to_data(root / f"base-{job.id}")
    db.add(JobEvent(job_id=job.id, stage="queued", progress=0, message="Шаблон поставлен в очередь"))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = db.scalar(select(Template).where(Template.owner_id == user.id, Template.sha256 == digest))
        if existing:
            return template_json(existing, _latest_analysis_job(db, existing.id))
        raise
    try:
        try:
            target_queue = queue("template-analysis")
        except TypeError:  # Compatibility with simple queue fakes.
            target_queue = queue()
        target_queue.enqueue(
            analyze_template, job.id, use_vlm, catalog_workers, False,
            job_id=f"template-analysis-{job.id}", job_timeout=900,
        )
    except RedisError as exc:
        job.status = template.status = "failed"
        job.error = template.error = "Очередь временно недоступна"
        db.commit()
        raise APIError(503, "queue_unavailable", "Очередь временно недоступна") from exc
    return template_json(template, job)


@router.delete("/templates/{template_id}", status_code=204)
def delete_template(
    template_id: str,
    user: User = Depends(current_user),
    db: DBSession = Depends(get_db),
):
    template = db.scalar(select(Template).where(
        Template.id == template_id,
        Template.owner_id == user.id,
        Template.status != "deleted",
    ).with_for_update())
    if not template:
        raise APIError(404, "not_found", "Шаблон не найден")
    active = db.scalar(select(Job.id).where(
        Job.template_id == template.id,
        Job.status.in_(["queued", "processing"]),
    ).limit(1))
    if active:
        raise APIError(
            409, "template_in_use",
            "Дождитесь завершения анализа или генерации с этим шаблоном",
        )

    template.status = "deleted"
    db.flush()
    root = user_root(user.id) / "templates" / template.id
    if root.exists():
        shutil.rmtree(root)
    db.commit()


@router.get("/templates/{template_id}")
def template_detail(template_id: str, user: User = Depends(current_user), db: DBSession = Depends(get_db)):
    template = _owned_template(db, user, template_id)
    return template_json(template, _latest_analysis_job(db, template.id))


@router.post("/templates/{template_id}/reanalyze", status_code=202)
def reanalyze_template(
    template_id: str,
    payload: TemplateAnalysisSettings,
    user: User = Depends(current_user),
    db: DBSession = Depends(get_db),
):
    template = _owned_template_for_update(db, user, template_id)
    latest = _latest_analysis_job(db, template.id)
    if latest and latest.status in {"queued", "processing"}:
        return template_json(template, latest)
    # Legacy active jobs did not pin a model version. They must finish before a
    # reanalysis can safely replace the path; all newly-created jobs are pinned.
    active_generations = db.scalars(select(Job).where(
        Job.template_id == template.id,
        Job.type == "generation",
        Job.status.in_(["queued", "processing"]),
    )).all()
    for active in active_generations:
        try:
            active_request = json.loads(
                (from_data(active.artifact_path or "") / "request.json").read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            active_request = {}
        if not active_request.get("model_path"):
            raise APIError(409, "template_in_use", "Дождитесь завершения генерации с этим шаблоном")
    had_ready_model = template.status == "ready" and bool(template.model_path) and from_data(template.model_path or "").is_dir()
    job = Job(owner_id=user.id, template_id=template.id, type="template_analysis", brief_excerpt=template.name)
    db.add(job)
    db.flush()
    if not had_ready_model:
        template.status = "processing"
    template.error = None
    root = user_root(user.id) / "templates" / template.id
    _prepare_analysis_request(
        job, template, root, use_vlm=payload.use_vlm,
        catalog_workers=payload.catalog_workers,
        enrichment_workers=payload.enrichment_workers,
        preserve_existing=had_ready_model,
    )
    db.add(JobEvent(job_id=job.id, stage="queued", progress=0, message="Повторный анализ поставлен в очередь"))
    db.commit()
    try:
        try:
            target_queue = queue("template-analysis")
        except TypeError:
            target_queue = queue()
        target_queue.enqueue(
            analyze_template, job.id, payload.use_vlm, payload.catalog_workers, had_ready_model,
            job_id=f"template-analysis-{job.id}", job_timeout=900,
        )
    except RedisError as exc:
        job.status, job.stage, job.error = "failed", "failed", "Очередь временно недоступна"
        template.status = "ready" if had_ready_model else "failed"
        template.error = job.error
        db.commit()
        raise APIError(503, "queue_unavailable", "Очередь временно недоступна") from exc
    return template_json(template, job)


@router.get("/templates/{template_id}/preview")
def template_preview(template_id: str, user: User = Depends(current_user), db: DBSession = Depends(get_db)):
    template = _owned_template(db, user, template_id)
    if template.status != "ready" or not template.model_path:
        raise APIError(409, "template_not_ready", "Превью ещё не готово")
    path = from_data(template.model_path) / "slides" / "slide_001" / "preview.png"
    if not path.is_file():
        raise APIError(404, "not_found", "Превью не найдено")
    return FileResponse(path, media_type="image/png")


@router.post("/jobs", status_code=202)
async def create_job(
    template_id: str = Form(...), brief: str = Form(...), content_text: str = Form(""),
    structured_assets: str = Form(""),
    slide_min: int = Form(10), slide_max: int = Form(15),
    generation_mode: str | None = Form(None), fast_mode: bool | None = Form(None),
    files: list[UploadFile] = File(default=[]),
    user: User = Depends(current_user), db: DBSession = Depends(get_db),
):
    template = _owned_template_for_update(db, user, template_id)
    if template.status != "ready":
        raise APIError(409, "template_not_ready", "Выберите готовый шаблон")
    brief = brief.strip()
    if not brief or len(brief) > 5000:
        raise APIError(422, "validation_error", "Бриф обязателен и не должен превышать 5000 символов", {"brief": "От 1 до 5000 символов"})
    if not 1 <= slide_min <= slide_max <= 30:
        raise APIError(422, "validation_error", "Диапазон слайдов должен быть от 1 до 30", {"slide_min": "Проверьте диапазон", "slide_max": "Проверьте диапазон"})
    if generation_mode is not None and generation_mode not in {"reliable", "strict"}:
        raise APIError(422, "validation_error", "generation_mode должен быть reliable или strict")
    resolved_mode = generation_mode or (
        "reliable" if fast_mode else "strict"
    )
    if len(files) > 10:
        raise APIError(413, "too_many_files", "Можно загрузить не более 10 файлов")
    if len(content_text) > settings.max_extracted_chars:
        raise APIError(413, "content_too_long", "Текст контент-пакета превышает 50 000 символов")
    parsed_assets = []
    if structured_assets.strip():
        try:
            parsed_assets = parse_json_assets(structured_assets)
        except StructuredAssetError as exc:
            raise APIError(422, "invalid_structured_assets", str(exc)) from exc
    if not content_text.strip() and not files and not parsed_assets:
        raise APIError(422, "validation_error", "Добавьте текст или хотя бы один файл", {"content_text": "Нужен контент-пакет"})
    job = Job(owner_id=user.id, template_id=template.id, type="generation", brief_excerpt=brief[:240])
    db.add(job)
    db.flush()
    root = user_root(user.id) / "jobs" / job.id
    assets = root / "assets"
    uploads = root / "uploads"
    assets.mkdir(parents=True)
    uploads.mkdir(parents=True)
    total = 0
    for upload in files:
        data = await read_limited(upload, settings.max_file_bytes)
        total += len(data)
        if total > settings.max_files_bytes:
            raise APIError(413, "content_too_large", "Суммарный размер файлов превышает 100 МБ")
        try:
            extension = validate_content_file(upload.filename or "", data)
        except ContentError as exc:
            raise APIError(422, "invalid_content_file", str(exc)) from exc
        target_dir = assets if extension in IMAGE_EXTENSIONS else uploads
        name = f"{secrets.token_hex(16)}{extension}"
        path = target_dir / name
        path.write_bytes(data)
        db.add(JobFile(
            job_id=job.id, original_name=Path(upload.filename or "file").name[:255],
            media_type=upload.content_type or "application/octet-stream",
            relative_path=relative_to_data(path), size=len(data),
        ))
    request_data = {
        "template_id": template.id,
        "model_path": template.model_path,
        "brief": brief,
        "content_text": content_text,
        "structured_assets": [asset.model_dump(mode="json") for asset in parsed_assets],
        "slide_min": slide_min,
        "slide_max": slide_max,
        "fast_mode": fast_mode,
        "generation_mode": resolved_mode,
    }
    (root / "request.json").write_text(json.dumps(request_data, ensure_ascii=False, indent=2), encoding="utf-8")
    job.artifact_path = relative_to_data(root)
    db.add(JobEvent(job_id=job.id, stage="queued", progress=0, message="Генерация поставлена в очередь"))
    db.commit()
    try:
        try:
            target_queue = queue("generation")
        except TypeError:
            target_queue = queue()
        target_queue.enqueue(
            generate_presentation, job.id,
            job_id=f"generation-{job.id}", job_timeout=settings.generation_job_timeout,
        )
    except RedisError as exc:
        job.status, job.stage, job.error = "failed", "failed", "Очередь временно недоступна"
        db.commit()
        raise APIError(503, "queue_unavailable", "Очередь временно недоступна") from exc
    return {"job_id": job.id}


@router.get("/jobs")
def jobs(cursor: str | None = None, limit: int = 20, user: User = Depends(current_user), db: DBSession = Depends(get_db)):
    limit = min(max(limit, 1), 50)
    query = select(Job).where(Job.owner_id == user.id, Job.type == "generation")
    if cursor:
        pivot = db.scalar(select(Job).where(Job.id == cursor, Job.owner_id == user.id))
        if pivot:
            query = query.where(or_(Job.created_at < pivot.created_at, and_(Job.created_at == pivot.created_at, Job.id < pivot.id)))
    values = db.scalars(query.order_by(Job.created_at.desc(), Job.id.desc()).limit(limit + 1)).all()
    next_cursor = values[limit - 1].id if len(values) > limit else None
    return {"items": [job_json(value) for value in values[:limit]], "next_cursor": next_cursor}


@router.get("/jobs/{job_id}")
def job_detail(job_id: str, user: User = Depends(current_user), db: DBSession = Depends(get_db)):
    return job_json(_owned_job(db, user, job_id))


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str, user: User = Depends(current_user), db: DBSession = Depends(get_db)):
    job = _owned_job_for_update(db, user, job_id)
    if job.type != "generation":
        raise APIError(409, "job_not_cancelable", "Эту задачу нельзя отменить")
    if job.status == "canceled":
        return job_json(job)
    if job.status not in {"queued", "processing"}:
        raise APIError(409, "job_not_active", "Задача уже завершена")
    job.status = job.stage = "canceled"
    job.error = None
    db.add(JobEvent(
        job_id=job.id, stage="canceled", progress=job.progress,
        message="Задача отменена пользователем",
    ))
    db.commit()
    try:
        _stop_rq_generation(job.id)
    except Exception:
        logger.exception("rq_cancel_failed job_id=%s", job.id)
    return job_json(job)


@router.get("/jobs/{job_id}/events")
async def job_events(
    job_id: str, request: Request, last_event_id: int | None = Header(default=None, alias="Last-Event-ID"),
    user: User = Depends(current_user), db: DBSession = Depends(get_db),
):
    _owned_job(db, user, job_id)

    async def stream():
        current = last_event_id or 0
        heartbeat = 0
        while not await request.is_disconnected():
            from .database import SessionLocal
            with SessionLocal() as event_db:
                events = event_db.scalars(select(JobEvent).where(JobEvent.job_id == job_id, JobEvent.id > current).order_by(JobEvent.id)).all()
                state = event_db.get(Job, job_id)
            for event in events:
                current = event.id
                yield f"id: {event.id}\nevent: job\ndata: {json.dumps(event_json(event), ensure_ascii=False)}\n\n"
            if state and state.status in {"ready", "failed", "canceled"} and not events:
                break
            heartbeat += 1
            if heartbeat % 5 == 0:
                yield ": heartbeat\n\n"
            await asyncio.sleep(2)
    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/jobs/{job_id}/download")
def download(job_id: str, user: User = Depends(current_user), db: DBSession = Depends(get_db)):
    job = _owned_job(db, user, job_id)
    path = from_data(job.artifact_path or "") / "presentation.pptx"
    if job.status != "ready" or not path.is_file():
        raise APIError(409, "job_not_ready", "Презентация ещё не готова")
    return FileResponse(path, filename=f"presentation-{job.id}.pptx", media_type="application/vnd.openxmlformats-officedocument.presentationml.presentation")


@router.get("/jobs/{job_id}/download/pdf")
def download_pdf(job_id: str, user: User = Depends(current_user), db: DBSession = Depends(get_db)):
    job = _owned_job(db, user, job_id)
    root = from_data(job.artifact_path or "")
    source = root / "presentation.pptx"
    path = root / "presentation.pdf"
    if job.status != "ready" or not source.is_file():
        raise APIError(409, "job_not_ready", "Презентация ещё не готова")
    if not path.is_file():
        try:
            render_pdf(source, path)
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            logger.exception("pdf_conversion_failed job_id=%s", job.id)
            raise APIError(
                503, "pdf_unavailable",
                "Не удалось подготовить PDF. Попробуйте скачать его ещё раз позже.",
            ) from exc
    return FileResponse(path, filename=f"presentation-{job.id}.pdf", media_type="application/pdf")


@router.get("/jobs/{job_id}/previews/{index}")
def preview(job_id: str, index: int, user: User = Depends(current_user), db: DBSession = Depends(get_db)):
    job = _owned_job(db, user, job_id)
    if job.status != "ready" or index < 0:
        raise APIError(409, "job_not_ready", "Превью ещё не готово")
    qa = from_data(job.qa_path or "")
    try:
        report = json.loads((qa / "report.json").read_text(encoding="utf-8"))
        relative = report["previews"][index]
    except (OSError, ValueError, KeyError, IndexError, TypeError) as exc:
        raise APIError(404, "not_found", "Превью не найдено") from exc
    path = (qa / relative).resolve()
    try:
        path.relative_to(qa.resolve())
    except ValueError as exc:
        raise APIError(404, "not_found", "Превью не найдено") from exc
    return FileResponse(path, media_type="image/png")
