"""Pydantic configuration models for DeerFlow's built-in cron scheduler.

This module is intentionally kept free of apscheduler imports.
Schedule expression validation is performed in app/cron/service.py
(inside setup_cron_service) to keep the harness dependency boundary clean.
"""

from pydantic import BaseModel, Field


class CronJobConfig(BaseModel):
    """Configuration for a single cron job."""

    id: str
    schedule: str                          # cron 表达式；合法性由 setup_cron_service 校验
    agent: str | None = None               # None 表示使用默认 lead_agent
    prompt: str
    enabled: bool = True
    timezone: str | None = None            # None 表示继承全局 timezone
    misfire_grace_time: int = 300          # 单位：秒；超出此窗口的错过任务将跳过
    coalesce: bool = True                  # 积压多次触发时是否合并为一次
    recursion_limit: int = 100             # LangGraph 递归步数限制（防止复杂任务超限中止）


class CronConfig(BaseModel):
    """Top-level cron configuration block."""

    enabled: bool = False
    timezone: str = "UTC"
    jobs: list[CronJobConfig] = Field(default_factory=list)
