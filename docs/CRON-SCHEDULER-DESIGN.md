# DeerFlow 内置定时任务机制设计方案

## 背景

DeerFlow 当前是纯被动响应式架构，仅在用户主动发起对话时执行 Agent。对于数据库运维等场景，需要支持定时自动触发 Agent 执行，例如：

- 每日凌晨自动生成数据库健康巡检报告
- 每周生成 AWR 趋势分析
- 定时检查慢查询、锁等待等告警指标

本方案以**最小代价**在现有 Gateway（FastAPI）内集成 APScheduler，复用已有的 LangGraph 调用链和配置体系，不引入新进程或外部依赖服务。

---

## 一、整体架构

```
Gateway (FastAPI)
└── lifespan startup
      └── CronService (APScheduler AsyncIOScheduler)
            ├── job: pg-daily-check  ──→ POST http://localhost:2024/threads
            ├── job: oracle-weekly   ──→ POST http://localhost:2024/threads/{id}/runs
            └── job: ...
```

定时任务的触发路径与用户手动发起对话的路径完全一致：

```
CronService
  → 创建新 thread（LangGraph API）
  → 提交 run（LangGraph API）
  → lead_agent 执行（含 custom agent 配置）
  → 输出写入 threads/{thread_id}/user-data/outputs/
```

### 与现有 IM Channel 机制的对比

DeerFlow 已有 Feishu/Telegram channel 的类似模式（在 lifespan 里启停服务），本方案沿用相同模式：

| | IM Channel Service | Cron Service（本方案） |
|---|---|---|
| 启动时机 | Gateway lifespan startup | Gateway lifespan startup |
| 停止时机 | Gateway lifespan shutdown | Gateway lifespan shutdown |
| 触发来源 | 外部消息平台 Webhook | 内置定时器 |
| 执行方式 | 调用 LangGraph API | 调用 LangGraph API |
| 配置入口 | `config.yaml` → `channels`（`model_extra`） | `config.yaml` → `cron`（typed field） |

---

## 二、目录结构

```
backend/
├── app/
│   ├── gateway/
│   │   ├── app.py               ← 在 lifespan 里加 cron 启停（约 10 行改动）
│   │   └── ...
│   └── cron/                    ← 新增模块
│       ├── __init__.py
│       ├── service.py           ← CronService 主体
│       └── trigger.py           ← 触发单次 agent run 的工具函数
├── packages/harness/deerflow/
│   └── config/
│       └── cron_config.py       ← CronConfig / CronJobConfig Pydantic 模型（纯 Pydantic，不依赖 apscheduler）
└── ...
```

> **依赖归属**：`apscheduler` 只加到 `backend/pyproject.toml`。`cron_config.py` 存放在 harness 包中（纯 Pydantic），不需要引入 apscheduler。

---

## 三、Config Schema

在 `config.yaml` 中新增 `cron` 配置节：

```yaml
cron:
  enabled: true
  # 默认时区，可被单个 job 覆盖
  timezone: Asia/Shanghai
  # LangGraph server 地址
  # 容器环境（docker-compose）中需改为 http://langgraph:2024
  # 也可通过环境变量 DEER_FLOW_CHANNELS_LANGGRAPH_URL 覆盖（与 channels 共用）

  jobs:
    - id: pg-daily-check
      schedule: "0 2 * * *"          # 标准 cron 表达式（分 时 日 月 周）
      agent: postgres-dba             # Custom Agent 名称（对应 .deer-flow/agents/ 下的目录名）
      prompt: "执行每日 PostgreSQL 健康巡检，生成报告并保存到 outputs"
      enabled: true
      timezone: Asia/Shanghai         # 可选，覆盖全局时区
      misfire_grace_time: 3600        # 1 小时内可补跑（日报类）

    - id: oracle-weekly-awr
      schedule: "0 3 * * 1"          # 每周一凌晨 3 点
      agent: oracle-dba
      prompt: "生成本周 Oracle AWR 趋势分析报告，重点关注 Top SQL 和等待事件"
      enabled: true
      misfire_grace_time: 7200        # 2 小时内可补跑

    - id: oracle-lock-monitor
      schedule: "*/30 * * * *"        # 每 30 分钟
      agent: oracle-dba
      prompt: "检查当前是否存在超过 5 分钟的阻塞锁，如有则生成告警报告"
      enabled: false                  # 暂时禁用
      misfire_grace_time: 60          # 告警类：1 分钟内补跑，超出跳过
      coalesce: true                  # 积压多次只触发一次
```

### Pydantic 模型（`deerflow/config/cron_config.py`）

`cron_config.py` 存放于 harness 包，**只依赖 Pydantic，不引入 apscheduler**。`schedule` 字段的合法性校验在 `setup_cron_service`（`app/cron/service.py`）里执行，保持两层代码的依赖边界清晰。

```python
from pydantic import BaseModel, Field


class CronJobConfig(BaseModel):
    id: str
    schedule: str                          # cron 表达式；合法性由 setup_cron_service 校验
    agent: str | None = None               # None 表示使用默认 lead_agent
    prompt: str
    enabled: bool = True
    timezone: str | None = None            # None 表示继承全局 timezone
    misfire_grace_time: int = 300          # 单位：秒；超出此窗口的错过任务将跳过
    coalesce: bool = True                  # 积压多次触发时是否合并为一次


class CronConfig(BaseModel):
    enabled: bool = False
    timezone: str = "UTC"
    jobs: list[CronJobConfig] = Field(default_factory=list)
```

### 集成到 `AppConfig`（跟随 `checkpointer` / `stream_bridge` 的 typed field 模式）

```python
# packages/harness/deerflow/config/app_config.py

from deerflow.config.cron_config import CronConfig   # 新增 import

class AppConfig(BaseModel):
    ...
    checkpointer: CheckpointerConfig | None = Field(default=None, ...)
    stream_bridge: StreamBridgeConfig | None = Field(default=None, ...)
    cron: CronConfig = Field(default_factory=CronConfig)   # 新增字段
```

`AppConfig.from_file()` 末尾的 `cls.model_validate(config_data)` 会自动解析 `cron` 节，**无需**在 `from_file()` 里额外添加 `load_cron_config_from_dict` 调用（CronService 直接通过 `get_app_config().cron` 访问，不依赖全局 singleton）。

> **与 `channels` 的区别**：`channels` 使用 `model_extra`（`extra="allow"`）读取，是历史设计；`cron` 跟随更新的 `checkpointer` 模式，使用 typed field，类型安全且无需 `model_extra` 访问。

---

## 四、核心实现

### 4.1 触发函数（`app/cron/trigger.py`）

```python
import logging
import os

import httpx

from app.channels.manager import DEFAULT_LANGGRAPH_URL

logger = logging.getLogger(__name__)

_LANGGRAPH_URL_ENV = "DEER_FLOW_CHANNELS_LANGGRAPH_URL"


def get_langgraph_url() -> str:
    """Resolve LangGraph URL: env var → DEFAULT_LANGGRAPH_URL.

    Reuses the same env var as IM channels to avoid duplicating config.
    """
    return os.getenv(_LANGGRAPH_URL_ENV, "").strip() or DEFAULT_LANGGRAPH_URL


_MAX_RETRIES = 3
_RETRY_BASE_DELAY = 2.0  # 指数退避基准秒数：2s → 4s → 8s


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
    """
    import asyncio

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
```

#### 已验证的设计决策

| 参数 | 结论 | 依据 |
|------|------|------|
| `configurable["agent_name"]` | ✅ 参数名正确 | `agent.py:287` `cfg.get("agent_name")`，`cfg = config["configurable"]` |
| `assistant_id: "lead_agent"` | ✅ 固定值，不随自定义 agent 变化 | `langgraph.json` 只注册一个图 `lead_agent`；Gateway 的 `assistants_compat.py` 虽为自定义 agent 创建了独立 `assistant_id`，但底层 `graph_id` 始终为 `lead_agent`（注释："All agents use the same graph"）。直接调用 LangGraph server（`:2024`）时必须用图名 `lead_agent`，自定义 agent 选择仅依赖 `configurable["agent_name"]` |

### 4.2 CronService（`app/cron/service.py`）

```python
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

    Args:
        config: Cron configuration from AppConfig.
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
```

### 4.3 集成到 Gateway lifespan（`app/gateway/app.py`）

在现有 `lifespan` 函数中加入 cron 启停（仿照 channel service 的模式）：

```python
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    ...
    async with langgraph_runtime(app):
        logger.info("LangGraph runtime initialised")

        # Start IM channel service（已有代码）
        ...

        # Start cron service（新增）
        # ValueError（非法 cron 表达式）故意不捕获，使 Gateway 启动失败并提示修正
        try:
            from app.cron.service import setup_cron_service
            setup_cron_service(get_app_config().cron)
        except ValueError:
            raise   # 配置错误 → fail-fast，让 Gateway 启动失败
        except Exception:
            logger.exception("Failed to start cron service")  # 其他意外错误 → 记录后继续

        yield

        # Stop cron service（新增，在 channel service 之前停止）
        try:
            from app.cron.service import stop_cron_service
            stop_cron_service()
        except Exception:
            logger.exception("Failed to stop cron service")

        # Stop channel service（已有代码）
        ...
```

---

## 五、依赖变更

在 `backend/pyproject.toml` 的 `[project.dependencies]` 中增加：

```toml
"apscheduler>=3.10,<4.0",   # 必须锁定 <4.0，4.x API 完全不兼容
"httpx>=0.27",               # 可能已存在，确认即可
```

> **注意**：APScheduler 4.x 已在 PyPI 发布，直接 `pip install apscheduler` 会装到 4.x。版本约束 `<4.0` 是必须的。

---

## 六、错误处理策略

| 场景 | 处理方式 |
|------|---------|
| LangGraph 服务未就绪时触发 | `trigger_agent_run` 指数退避重试最多 3 次（2s/4s/8s），全部失败后 APScheduler 捕获异常记录日志，不影响后续调度 |
| Cron 表达式非法 | `setup_cron_service` 在启动前逐一调用 `CronTrigger.from_crontab()` 验证，抛出 `ValueError`；`lifespan` 对 `ValueError` 不捕获（`except ValueError: raise`），Gateway 启动失败并在日志中给出明确 job id 和 schedule 值；`ImportError`（apscheduler 未安装）正常传播不被吞掉 |
| 单次 job 执行失败（HTTP 5xx 等） | 重试耗尽后 APScheduler 捕获异常记录日志，下次 schedule 时间正常触发 |
| `stop_cron_service(wait=True)` | 等待当前正在执行的 job 完成后再停止，避免 DB 操作被强制中断产生孤儿 thread |

### `misfire_grace_time` 与 `coalesce` 的区别

这两个参数容易混淆，分别控制不同的场景：

**`misfire_grace_time`（单位：秒）**

当 APScheduler 检测到某次触发已经"错过"（即到了触发时间但调度器因停机/忙碌未能及时执行），它会检查当前时间与计划触发时间之差：
- 差值 ≤ `misfire_grace_time`：补跑这次错过的任务
- 差值 > `misfire_grace_time`：直接跳过，等待下一次正常触发

```
# 示例：每日 02:00 的任务，服务器 02:30 才重启
misfire_grace_time=3600  → 差值 30min < 1h → 补跑 ✅
misfire_grace_time=60    → 差值 30min > 1min → 跳过 ❌
```

**`coalesce=True`**

当 APScheduler 发现**同一个 job** 由于错过而积压了多次待执行记录（例如宕机 3 小时，期间应该触发了 6 次的 30 分钟任务），`coalesce=True` 将这 6 次合并为只跑 1 次，而不是补跑 6 次。

| | `misfire_grace_time` | `coalesce` |
|---|---|---|
| 控制的问题 | 错过多久还要补跑 | 积压多次是否合并为一次 |
| 不设置的后果 | 错过的任务永远补跑（可能不合适） | 积压 N 次就跑 N 次（任务堆积） |
| 日报类推荐 | 3600（1 小时窗口） | `true` |
| 告警类推荐 | 60（超过 1 分钟直接跳过） | `true` |

---

## 六-A、已知限制

以下是设计阶段识别的固有限制，当前版本不解决，列出供实现者知悉：

**1. 触发成功 ≠ 执行成功（fire-and-forget）**

`trigger_agent_run` 只保证 run 被成功**提交**到 LangGraph（HTTP 200），不等待 Agent 执行完成。Agent 可能在执行中途因模型错误、工具超时等原因失败，CronService 对此无感知。

缓解方式：运维人员定期检查 `logs/gateway.log` 中的 `[CRON] Job triggered` 与实际 outputs 目录文件数量是否匹配。

**2. Thread 目录无限积累**

每次 cron 触发产生一个独立的 thread 目录（`backend/.deer-flow/threads/{uuid}/`），长期运行后目录数量无上限。当前版本无自动清理机制。

缓解方式：参见第八节扩展路径"Thread 目录清理"，可将清理本身实现为一个特殊 cron job。

**3. Gateway 与 LangGraph 的启动竞态窗口**

`trigger_agent_run` 的重试逻辑（最多 3 次，最长等待 14s）只覆盖 job 触发时 LangGraph 未就绪的情况。如果 Gateway 刚启动后立即有 cron 触发（例如 `schedule: "* * * * *"`），且 LangGraph 超过 14s 才就绪，这次触发仍会失败。

缓解方式：`_MAX_RETRIES` 和 `_RETRY_BASE_DELAY` 可根据实际 LangGraph 启动时间调整；或在 `setup_cron_service` 里延迟首次调度（`next_run_time` 参数）。

---

## 七、运维说明

### 查看 cron 执行日志

所有 cron 相关日志由 `app.cron` logger 输出，写入 `logs/gateway.log`：

```bash
# 查看所有 cron 相关日志（大小写不敏感）
grep -i "\[cron\]" logs/gateway.log

# 查看 job 触发记录，包含 thread_id 和 run_id
grep -i "job triggered" logs/gateway.log

# 查看调度配置加载
grep -i "scheduled cron job" logs/gateway.log
```

### 追溯 cron 输出文件

每次 cron 触发会在日志中打印 `thread_id`，根据此 ID 找到输出目录：

```bash
# 从日志获取最近一次 pg-daily-check 的 thread_id
grep "pg-daily-check" logs/gateway.log | grep "triggered" | tail -1

# 查看对应 outputs 目录
ls backend/.deer-flow/threads/<thread_id>/user-data/outputs/
```

### 动态开关 job

修改 `config.yaml` 中对应 job 的 `enabled: false`，重启 Gateway（`make stop && make dev-daemon`）生效。

> APScheduler 当前实现不支持热重载配置，需重启 Gateway。如需热重载，可后续扩展管理 API（`POST /api/cron/jobs/{id}/pause`）。

### 容器环境（docker-compose）的 URL 配置

容器内 Gateway 访问 LangGraph 需使用服务名而非 localhost：

```bash
# 在 .env 或 docker-compose.yaml 中设置
DEER_FLOW_CHANNELS_LANGGRAPH_URL=http://langgraph:2024
```

CronService 与 IM channels 共用此环境变量，无需额外配置。

---

## 八、扩展路径

本方案刻意保持最小实现，后续可按需扩展：

- **热重载**：监听 `config.yaml` 变更，自动 reschedule（`AppConfig` 已有 mtime 检测机制）
- **管理 API**：`/api/cron/jobs` 提供暂停/恢复/立即触发/查看下次执行时间接口
- **执行历史**：将每次 job 执行的 thread_id、run_id、状态、时间写入 SQLite
- **回调通知**：job 完成后通过 IM channel 发送报告摘要（结合 Telegram/Feishu）
- **Thread 目录清理**：定期清理 N 天前的 cron thread 目录（可作为一个特殊 cron job 实现）

---

## 九、实施检查清单

- [ ] 新增 `deerflow/config/cron_config.py`（纯 Pydantic 模型，不依赖 apscheduler；cron 表达式合法性由 `setup_cron_service` 校验）
- [ ] 在 `AppConfig` 中增加 `cron: CronConfig = Field(default_factory=CronConfig)` 字段及对应 import
- [ ] 新增 `app/cron/__init__.py`、`app/cron/trigger.py`、`app/cron/service.py`
- [ ] 修改 `app/gateway/app.py` lifespan，加入 cron 启停（约 10 行）
- [ ] 在 `backend/pyproject.toml` 增加 `"apscheduler>=3.10,<4.0"` 依赖
- [ ] 更新 `config.example.yaml`，增加 `cron` 配置节示例（含 `misfire_grace_time` 示例）
- [ ] 在 `config.yaml` 中按需配置实际的 cron jobs
- [ ] 新增 `tests/test_cron_config.py`（Pydantic 模型校验；验证 `cron_config.py` 无 apscheduler 导入副作用）
- [ ] 新增 `tests/test_cron_service.py`（setup/stop 幂等性、禁用 job 的处理、非法 schedule 抛出 ValueError）
