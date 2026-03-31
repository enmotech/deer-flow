"""CronService: APScheduler-based cron scheduler for DeerFlow Gateway."""

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from deerflow.config.cron_config import CronConfig
from .trigger import trigger_agent_run

logger = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None


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
        logger.warning("Cron service already running, skipping setup")
        return

    if not config.enabled or not config.jobs:
        logger.info("Cron service disabled or no jobs configured, skipping")
        return

    # Validate all enabled job schedules before starting the scheduler.
    # Done here (not in CronJobConfig) to keep harness free of apscheduler dependency.
    for job in config.jobs:
        if not job.enabled:
            continue
        try:
            CronTrigger.from_crontab(job.schedule)
        except (ValueError, TypeError) as exc:
            raise ValueError(
                f"Cron job '{job.id}' has invalid schedule '{job.schedule}': {exc}"
            ) from exc

    _scheduler = AsyncIOScheduler()

    enabled_count = 0
    for job in config.jobs:
        if not job.enabled:
            logger.debug("Cron job %s is disabled, skipping", job.id)
            continue

        timezone = job.timezone or config.timezone

        _scheduler.add_job(
            trigger_agent_run,
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
            "Scheduled cron job: id=%s schedule='%s' agent=%s tz=%s misfire_grace=%ds",
            job.id, job.schedule, job.agent or "default", timezone, job.misfire_grace_time,
        )
        enabled_count += 1

    if enabled_count == 0:
        logger.info("No enabled cron jobs found")
        _scheduler = None
        return

    _scheduler.start()
    logger.info("Cron service started with %d job(s)", enabled_count)


def stop_cron_service() -> None:
    """Shut down the cron scheduler, waiting for running jobs to complete."""
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=True)   # 等待正在执行的 job 完成，避免半完成报告
        logger.info("Cron service stopped")
    _scheduler = None
