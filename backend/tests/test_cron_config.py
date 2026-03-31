"""Unit tests for CronConfig / CronJobConfig Pydantic models."""

import pytest
from pydantic import ValidationError

from deerflow.config.cron_config import CronConfig, CronJobConfig


class TestCronJobConfig:
    def test_minimal_valid_job(self):
        job = CronJobConfig(id="test", schedule="0 2 * * *", prompt="hello")
        assert job.id == "test"
        assert job.enabled is True
        assert job.misfire_grace_time == 300
        assert job.coalesce is True
        assert job.agent is None
        assert job.timezone is None

    def test_agent_and_timezone_optional(self):
        job = CronJobConfig(
            id="j", schedule="*/5 * * * *", prompt="p",
            agent="my-agent", timezone="UTC",
        )
        assert job.agent == "my-agent"
        assert job.timezone == "UTC"

    def test_disabled_job(self):
        job = CronJobConfig(id="j", schedule="0 1 * * *", prompt="p", enabled=False)
        assert job.enabled is False

    def test_missing_prompt_raises(self):
        with pytest.raises(ValidationError):
            CronJobConfig(id="j", schedule="0 1 * * *")  # prompt is required

    def test_coalesce_false(self):
        job = CronJobConfig(id="j", schedule="0 1 * * *", prompt="p", coalesce=False)
        assert job.coalesce is False

    def test_custom_misfire_grace_time(self):
        job = CronJobConfig(id="j", schedule="0 1 * * *", prompt="p", misfire_grace_time=3600)
        assert job.misfire_grace_time == 3600


class TestCronConfig:
    def test_defaults(self):
        cfg = CronConfig()
        assert cfg.enabled is False
        assert cfg.timezone == "UTC"
        assert cfg.jobs == []

    def test_with_jobs(self):
        cfg = CronConfig(
            enabled=True,
            timezone="Asia/Tokyo",
            jobs=[{"id": "j1", "schedule": "0 1 * * *", "prompt": "p"}],
        )
        assert cfg.enabled is True
        assert len(cfg.jobs) == 1
        assert cfg.jobs[0].id == "j1"

    def test_no_apscheduler_import(self):
        """Verify cron_config has zero apscheduler dependency at import time."""
        import deerflow.config.cron_config as mod
        # Check only the names defined/imported directly in the module namespace
        member_names = set(vars(mod).keys())
        assert not any("apscheduler" in name for name in member_names), (
            "cron_config.py must not import apscheduler at module level"
        )
