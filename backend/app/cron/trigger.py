"""Trigger function: creates a LangGraph thread and submits an agent run."""

import asyncio
import logging
import os

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
    recursion_limit: int = 100,
) -> str:
    """Create a new thread and submit a run to LangGraph.

    Retries up to _MAX_RETRIES times with exponential backoff to tolerate
    LangGraph startup race conditions (Gateway may start before LangGraph).

    Args:
        prompt: User message to send to the agent.
        agent: Custom agent name. None uses the default lead_agent.
        recursion_limit: Max number of nodes allowed for this run.

    Returns:
        The thread_id of the created thread.

    Raises:
        Exception: Re-raises the last exception after all retries are exhausted.
        RuntimeError: If _MAX_RETRIES is 0 (no attempts were made).
    """
    from langgraph_sdk import get_client

    langgraph_url = get_langgraph_url()
    client = get_client(url=langgraph_url)

    # Configurable sets agent_name if custom agent requested.
    configurable: dict = {}
    if agent:
        configurable["agent_name"] = agent

    # LangGraph Config object containing recursion_limit and configurable.
    run_config = {
        "recursion_limit": recursion_limit,
        "configurable": configurable,
    }

    last_exc: Exception | None = None
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            # 1. 创建新 thread
            thread = await client.threads.create()
            thread_id = thread["thread_id"]

            # 2. 提交 run (background)
            run = await client.runs.create(
                thread_id,
                "lead_agent",
                input={"messages": [{"role": "user", "content": prompt}]},
                config=run_config,
            )
            run_id = run["run_id"]

            logger.info(
                "[CRON] Job triggered: thread_id=%s run_id=%s agent=%s recursion_limit=%d",
                thread_id,
                run_id,
                agent or "default",
                recursion_limit,
            )
            return thread_id

        except Exception as exc:
            last_exc = exc
            if attempt < _MAX_RETRIES:
                delay = _RETRY_BASE_DELAY**attempt
                logger.warning(
                    "[CRON] trigger_agent_run failed (attempt %d/%d), retrying in %.0fs: %s",
                    attempt,
                    _MAX_RETRIES,
                    delay,
                    exc,
                )
                await asyncio.sleep(delay)
            else:
                logger.error(
                    "[CRON] trigger_agent_run failed after %d attempts: %s",
                    _MAX_RETRIES,
                    exc,
                )

    if last_exc is not None:
        raise last_exc
    raise RuntimeError("trigger_agent_run: no attempts were made (_MAX_RETRIES=0)")
