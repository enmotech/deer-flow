"""CronService: APScheduler-based cron scheduler for DeerFlow Gateway."""

import asyncio
import logging
from typing import Set

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from deerflow.config.cron_config import CronConfig
from .trigger import trigger_agent_run

logger = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None
_active_tasks: Set[asyncio.Task] = set()


async def _run_job_with_tracking(prompt: str, agent: str | None = None) -> None:
    """Wrapper to track the active task during execution."""
    task = asyncio.current_task()
    if task:
        _active_tasks.add(task)
    try:
        await trigger_agent_run(prompt, agent)
    finally:
        if task:
            _active_tasks.discard(task)


def _add_job_to_scheduler(
    scheduler: AsyncIOScheduler,
    job,  # CronJobConfig
    global_timezone: str,
) -> None:
    """Add a single cron job to the scheduler instance."""
    timezone = job.timezone or global_timezone

    scheduler.add_job(
        _run_job_with_tracking,
        CronTrigger.from_crontab(job.schedule, timezone=timezone),
        id=job.id,
        name=job.id,
        kwargs={
            "prompt": job.prompt,
            "agent": job.agent,
        },
        misfire_grace_time=job.misfire_grace_time,
        coalesce=job.coalesce,
    )
    logger.info(
        "[CRON] Scheduled cron job: id=%s schedule='%s' agent=%s tz=%s misfire_grace=%ds",
        job.id, job.schedule, job.agent or "default", timezone, job.misfire_grace_time,
    )


def setup_cron_service(config: CronConfig) -> None:
    """Initialise and start the cron scheduler.

    Idempotent: safe to call multiple times; subsequent calls are no-ops.

    Validates all enabled job schedules before starting the scheduler.
    Raises ValueError (with job id and schedule value) if any schedule is
    invalid, which will propagate to Gateway lifespan and fail startup
    (fail-fast behaviour — see app/gateway/app.py lifespan).

    Args:
        config: Cron configuration from AppConfig.

    Raises:
        ValueError: If any enabled job has an invalid cron schedule expression.
    """
    global _scheduler

    # 幂等保护：已运行则跳过
    if _scheduler is not None and _scheduler.running:
        logger.warning("[CRON] Cron service already running, skipping setup")
        return

    if not config.enabled or not config.jobs:
        logger.info("[CRON] Cron service disabled or no jobs configured, skipping")
        return

    # Validate all enabled job schedules before starting the scheduler.
    # Done here (not in CronJobConfig) to keep harness free of apscheduler dependency.
    for job in config.jobs:
        if not job.enabled:
            continue
        try:
            CronTrigger.from_crontab(
                job.schedule, timezone=job.timezone or config.timezone
            )
        except (ValueError, TypeError) as exc:
            raise ValueError(
                f"Cron job '{job.id}' has invalid schedule '{job.schedule}': {exc}"
            ) from exc

    _scheduler = AsyncIOScheduler()

    enabled_count = 0
    for job in config.jobs:
        if not job.enabled:
            logger.debug("[CRON] Cron job %s is disabled, skipping", job.id)
            continue

        _add_job_to_scheduler(_scheduler, job, config.timezone)
        enabled_count += 1

    if enabled_count == 0:
        logger.info("[CRON] No enabled cron jobs found")
        _scheduler = None
        return

    _scheduler.start()
    logger.info("[CRON] Cron service started with %d job(s)", enabled_count)


async def stop_cron_service() -> None:
    """Shut down the cron scheduler, waiting for running jobs to complete."""
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=True)
        logger.info("[CRON] Cron scheduler shut down")

    # APScheduler 3.x shutdown(wait=True) doesn't wait for async tasks.
    # We manually wait for our tracked active tasks.
    if _active_tasks:
        logger.info("[CRON] Waiting for %d active cron task(s) to complete...", len(_active_tasks))
        await asyncio.gather(*_active_tasks, return_exceptions=True)
        _active_tasks.clear()

    _scheduler = None
    logger.info("[CRON] Cron service stopped")
