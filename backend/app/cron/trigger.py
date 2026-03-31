"""Trigger function: creates a LangGraph thread and submits an agent run."""

import asyncio
import logging
import os

import httpx

from app.channels.manager import DEFAULT_LANGGRAPH_URL

logger = logging.getLogger(__name__)

_LANGGRAPH_URL_ENV = "DEER_FLOW_CHANNELS_LANGGRAPH_URL"

_MAX_RETRIES = 3
_RETRY_BASE_DELAY = 2.0  # 指数退避基准秒数：2s → 4s → 8s


def get_langgraph_url() -> str:
    """Resolve LangGraph URL: env var → DEFAULT_LANGGRAPH_URL.

    Reuses the same env var as IM channels to avoid duplicating config.
    """
    return os.getenv(_LANGGRAPH_URL_ENV, "").strip() or DEFAULT_LANGGRAPH_URL


async def trigger_agent_run(
    prompt: str,
    agent: str | None = None,
) -> str:
    """Create a new thread and submit a run to LangGraph.

    Retries up to _MAX_RETRIES times with exponential backoff to tolerate
    LangGraph startup race conditions (Gateway may start before LangGraph).

    Args:
        prompt: User message to send to the agent.
        agent: Custom agent name. None uses the default lead_agent.

    Returns:
        The thread_id of the created thread.

    Raises:
        Exception: Re-raises the last exception after all retries are exhausted.
        RuntimeError: If _MAX_RETRIES is 0 (no attempts were made).
    """
    langgraph_url = get_langgraph_url()
    configurable: dict = {}
    if agent:
        configurable["agent_name"] = agent

    last_exc: Exception | None = None
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                # 1. 创建新 thread
                resp = await client.post(f"{langgraph_url}/threads", json={})
                resp.raise_for_status()
                thread_id: str = resp.json()["thread_id"]

                # 2. 提交 run（fire-and-forget，不等待执行完成）
                run_resp = await client.post(
                    f"{langgraph_url}/threads/{thread_id}/runs",
                    json={
                        "assistant_id": "lead_agent",
                        "input": {
                            "messages": [{"role": "user", "content": prompt}]
                        },
                        "config": {"configurable": configurable} if configurable else {},
                    },
                )
                run_resp.raise_for_status()
                run_id: str = run_resp.json()["run_id"]

            logger.info(
                "[CRON] Job triggered: thread_id=%s run_id=%s agent=%s",
                thread_id, run_id, agent or "default",
            )
            return thread_id

        except Exception as exc:
            last_exc = exc
            if attempt < _MAX_RETRIES:
                delay = _RETRY_BASE_DELAY ** attempt
                logger.warning(
                    "[CRON] trigger_agent_run failed (attempt %d/%d), retrying in %.0fs: %s",
                    attempt, _MAX_RETRIES, delay, exc,
                )
                await asyncio.sleep(delay)
            else:
                logger.error(
                    "[CRON] trigger_agent_run failed after %d attempts: %s",
                    _MAX_RETRIES, exc,
                )

    if last_exc is not None:
        raise last_exc
    raise RuntimeError("trigger_agent_run: no attempts were made (_MAX_RETRIES=0)")
