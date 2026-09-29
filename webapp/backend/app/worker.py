from __future__ import annotations

import json
import logging

from redis import Redis
from rq import Queue, Worker
from rq.registry import StartedJobRegistry
from sqlalchemy import select

from .config import settings
from .database import SessionLocal
from .models import Job, JobEvent
from .storage import from_data
from .tasks import _atomic_write_text, analyze_template, enrich_template, generate_presentation


logger = logging.getLogger("webapp.worker")


def recover_stale_jobs(connection: Redis, queue_name: str) -> None:
    registry = StartedJobRegistry(queue_name, connection=connection)
    timeout = 3600 if queue_name == "template-enrichment" else (900 if queue_name == "template-analysis" else settings.generation_job_timeout)
    queue = Queue(queue_name, connection=connection, default_timeout=timeout)
    active = set(registry.get_job_ids()) | set(queue.get_job_ids())
    job_type = "generation" if queue_name == "generation" else "template_analysis"
    task = {
        "generation": generate_presentation,
        "template-analysis": analyze_template,
        "template-enrichment": enrich_template,
    }[queue_name]
    stages = {
        "template-analysis": {
            "validating", "analyzing_structure", "building_base_catalog",
            "analyzing_structure_and_vlm", "building_catalog",
        },
        "template-enrichment": {"ready_enriching", "enriching_visuals", "rebuilding_catalog"},
    }.get(queue_name)
    with SessionLocal() as db:
        stale = list(db.scalars(select(Job).where(
            Job.type == job_type, Job.status == "processing",
        )))
        for job in stale:
            if stages is not None and job.stage not in stages:
                continue
            rq_id = f"{queue_name}-{job.id}"
            if rq_id in active:
                continue
            request_path = from_data(job.artifact_path or "") / "request.json"
            try:
                request = json.loads(request_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                request = {}
            markers = [item for item in (job.warnings or []) if isinstance(item, dict)]
            resumes = max(
                int(request.get("system_resume_count", 0)),
                sum(item.get("code") == "system_resume" for item in markers),
            )
            if resumes >= 1:
                if queue_name == "template-enrichment":
                    job.status, job.stage = "ready", "enrichment_failed"
                    job.warnings = [*(job.warnings or []), "Повторный инфраструктурный сбой визуального анализа."]
                else:
                    job.status = job.stage = "failed"
                    job.error = "Повторный инфраструктурный сбой; запустите задачу заново."
                db.add(JobEvent(
                    job_id=job.id, stage=job.stage, progress=job.progress,
                    message="Задача остановлена после повторного системного сбоя",
                ))
                continue
            request["system_resume_count"] = resumes + 1
            job.warnings = [*(job.warnings or []), {
                "code": "system_resume", "path": "", "message": "worker restart recovery",
            }]
            if job.artifact_path and request_path.parent.is_dir():
                _atomic_write_text(
                    request_path, json.dumps(request, ensure_ascii=False, indent=2) + "\n",
                )
            job.status, job.stage = "queued", "resuming"
            db.add(JobEvent(
                job_id=job.id, stage="resuming", progress=job.progress,
                message="Возобновляем задачу с последней контрольной точки",
            ))
            queue.enqueue(task, job.id, job_id=rq_id, job_timeout=timeout, retry=None)
        db.commit()


def work_horse_killed_handler(rq_job, retpid, ret_val, rusage) -> None:
    """Reflect hard timeouts/OOM kills even when the task cannot catch an exception."""
    args = getattr(rq_job, "args", ())
    if not args:
        return
    job_id = str(args[0])
    rq_id = str(getattr(rq_job, "id", ""))
    with SessionLocal() as db:
        job = db.get(Job, job_id)
        if not job or job.status not in {"queued", "processing"}:
            return
        if rq_id.startswith("template-enrichment-"):
            job.status, job.stage = "ready", "enrichment_failed"
            message = "Визуальное обогащение прервано по системному таймауту; базовая версия доступна."
            job.warnings = [*(job.warnings or []), message]
        else:
            job.status, job.stage = "failed", "failed"
            message = "Обработка аварийно завершена по системному таймауту."
            job.error = message
            if job.type == "template_analysis" and job.template:
                published = bool(
                    job.template.model_path
                    and from_data(job.template.model_path).is_dir()
                )
                job.template.status = "ready" if published else "failed"
                job.template.error = None if published else message
        db.add(JobEvent(job_id=job.id, stage=job.stage, progress=job.progress, message=message))
        db.commit()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    connection = Redis.from_url(settings.redis_url)
    queue_name = settings.worker_queue
    if queue_name not in {"generation", "template-analysis", "template-enrichment"}:
        raise ValueError(f"unsupported worker queue {queue_name!r}")
    recover_stale_jobs(connection, queue_name)
    timeout = 3600 if queue_name == "template-enrichment" else (900 if queue_name == "template-analysis" else settings.generation_job_timeout)
    Worker(
        [Queue(queue_name, connection=connection, default_timeout=timeout)],
        connection=connection,
        work_horse_killed_handler=work_horse_killed_handler,
    ).work()


if __name__ == "__main__":
    main()
