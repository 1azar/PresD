from redis import Redis
from rq import Queue

from .config import settings


def queue(name: str = "generation") -> Queue:
    if name not in {"generation", "template-analysis", "template-enrichment"}:
        raise ValueError(f"unknown queue {name!r}")
    timeout = 3600 if name == "template-enrichment" else (900 if name == "template-analysis" else settings.generation_job_timeout)
    return Queue(name, connection=Redis.from_url(settings.redis_url), default_timeout=timeout)
