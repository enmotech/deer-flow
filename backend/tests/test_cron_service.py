"""Unit tests for CronService: setup/stop idempotency, job filtering, schedule validation."""

from unittest.mock import patch

import pytest

from deerflow.config.cron_config import CronConfig, CronJobConfig


@pytest.fixture(autouse=True)
async def reset_scheduler():
    """Reset the global _scheduler before and after each test."""
    import app.cron.service as svc
    svc._scheduler = None
    svc._active_tasks.clear()
    yield
    await svc.stop_cron_service()
    svc._scheduler = None
    svc._active_tasks.clear()


@pytest.mark.anyio
class TestSetupCronService:
    async def test_disabled_config_is_noop(self):
        from app.cron.service import setup_cron_service
        import app.cron.service as svc

        setup_cron_service(CronConfig(enabled=False))
        assert svc._scheduler is None

    async def test_empty_jobs_is_noop(self):
        from app.cron.service import setup_cron_service
        import app.cron.service as svc

        setup_cron_service(CronConfig(enabled=True, jobs=[]))
        assert svc._scheduler is None

    async def test_all_jobs_disabled_is_noop(self):
        from app.cron.service import setup_cron_service
        import app.cron.service as svc

        cfg = CronConfig(
            enabled=True,
            jobs=[CronJobConfig(id="j", schedule="0 1 * * *", prompt="p", enabled=False)],
        )
        setup_cron_service(cfg)
        assert svc._scheduler is None

    async def test_invalid_schedule_raises_value_error(self):
        from app.cron.service import setup_cron_service

        cfg = CronConfig(
            enabled=True,
            jobs=[CronJobConfig(id="bad-job", schedule="not-a-cron", prompt="p")],
        )
        with pytest.raises(ValueError, match="bad-job"):
            setup_cron_service(cfg)

    async def test_invalid_schedule_error_contains_schedule_value(self):
        from app.cron.service import setup_cron_service

        cfg = CronConfig(
            enabled=True,
            jobs=[CronJobConfig(id="j", schedule="invalid", prompt="p")],
        )
        with pytest.raises(ValueError, match="invalid"):
            setup_cron_service(cfg)

    async def test_valid_job_creates_scheduler(self):
        """Scheduler object is created and add_job is called (start is mocked)."""
        from app.cron.service import setup_cron_service
        import app.cron.service as svc

        cfg = CronConfig(
            enabled=True,
            jobs=[CronJobConfig(id="j1", schedule="0 2 * * *", prompt="hello")],
        )
        with patch("app.cron.service.AsyncIOScheduler") as MockScheduler:
            mock_instance = MockScheduler.return_value
            mock_instance.running = False
            setup_cron_service(cfg)
            # 验证注册的是包裹函数 _run_job_with_tracking 而非原始函数
            from app.cron.service import _run_job_with_tracking
            mock_instance.add_job.assert_called_once()
            args, kwargs = mock_instance.add_job.call_args
            assert args[0] == _run_job_with_tracking
            mock_instance.start.assert_called_once()

    async def test_job_registered_with_correct_id(self):
        from app.cron.service import setup_cron_service

        cfg = CronConfig(
            enabled=True,
            jobs=[CronJobConfig(id="my-job", schedule="0 3 * * *", prompt="p")],
        )
        with patch("app.cron.service.AsyncIOScheduler") as MockScheduler:
            mock_instance = MockScheduler.return_value
            mock_instance.running = False
            setup_cron_service(cfg)
            call_kwargs = mock_instance.add_job.call_args
            assert call_kwargs.kwargs["id"] == "my-job"

    async def test_idempotent_second_call_is_noop(self):
        from app.cron.service import setup_cron_service
        import app.cron.service as svc

        cfg = CronConfig(
            enabled=True,
            jobs=[CronJobConfig(id="j1", schedule="0 2 * * *", prompt="hello")],
        )
        with patch("app.cron.service.AsyncIOScheduler") as MockScheduler:
            mock_instance = MockScheduler.return_value
            mock_instance.running = False
            setup_cron_service(cfg)
            svc._scheduler = mock_instance
            mock_instance.running = True  # 第二次调用时 running=True

            setup_cron_service(cfg)  # second call must be a no-op
            assert mock_instance.add_job.call_count == 1  # add_job 只被调用一次

    async def test_disabled_job_not_registered(self):
        from app.cron.service import setup_cron_service

        cfg = CronConfig(
            enabled=True,
            jobs=[
                CronJobConfig(id="active", schedule="0 2 * * *", prompt="p"),
                CronJobConfig(id="inactive", schedule="0 3 * * *", prompt="p", enabled=False),
            ],
        )
        with patch("app.cron.service.AsyncIOScheduler") as MockScheduler:
            mock_instance = MockScheduler.return_value
            mock_instance.running = False
            setup_cron_service(cfg)
            # add_job 只被调用一次（active），inactive 被跳过
            assert mock_instance.add_job.call_count == 1
            call_kwargs = mock_instance.add_job.call_args
            assert call_kwargs.kwargs["id"] == "active"

    async def test_timezone_inheritance(self):
        """Job without explicit timezone should use global timezone when registering."""
        from app.cron.service import setup_cron_service

        cfg = CronConfig(
            enabled=True,
            timezone="Asia/Tokyo",
            jobs=[CronJobConfig(id="j", schedule="0 1 * * *", prompt="p")],
        )
        with patch("app.cron.service.AsyncIOScheduler") as MockScheduler:
            mock_instance = MockScheduler.return_value
            mock_instance.running = False
            with patch("app.cron.service.CronTrigger") as MockTrigger:
                setup_cron_service(cfg)
                calls = MockTrigger.from_crontab.call_args_list
                # 现在校验循环和添加循环都会传入 timezone
                # validation loop: from_crontab(schedule, timezone=tz)
                # add_job loop:     from_crontab(schedule, timezone=tz)
                assert len(calls) == 2
                assert calls[0].kwargs.get("timezone") == "Asia/Tokyo"
                assert calls[1].kwargs.get("timezone") == "Asia/Tokyo"


@pytest.mark.anyio
class TestStopCronService:
    async def test_stop_when_not_started_is_safe(self):
        from app.cron.service import stop_cron_service

        await stop_cron_service()  # should not raise

    async def test_stop_clears_scheduler(self):
        from app.cron.service import stop_cron_service
        import app.cron.service as svc

        mock_scheduler = patch("app.cron.service._scheduler").start()
        mock_scheduler.running = True
        svc._scheduler = mock_scheduler

        await stop_cron_service()

        assert svc._scheduler is None
        mock_scheduler.shutdown.assert_called_once_with(wait=True)
        patch.stopall()
