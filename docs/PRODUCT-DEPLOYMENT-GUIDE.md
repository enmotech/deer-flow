# DeerFlow 数据库运维 Agent 产品部署指南

本文档描述如何将 DeerFlow 打包成可交付的数据库运维 Agent 产品，并指导在客户现场进行部署、启动、运维以及理解 Agent 编排模型。

---

## 一、核心设计原则

### 1.1 三层分离

本产品的整体设计按变更频率分为三层，不同层采用不同的交付和管理方式：

| 层次 | 内容 | 变更频率 | 管理方式 |
|------|------|---------|---------|
| **软件层（Image）** | DeerFlow 服务 + 数据库 CLI 工具 | 随版本发布 | 构建新 Image，发布镜像仓库 |
| **业务知识层（Skills）** | 运维 Skill 定义（SKILL.md 文件） | 频繁迭代 | 直接修改共享卷，无需重建 Image |
| **客户配置层（注入）** | 凭据、连接配置、LLM API Key | 按需更新 | Volume 挂载，不进入 Image |

### 1.2 为什么 CLI 要打包进 Image

数据库运维 Agent 的核心能力依赖 CLI 工具（psql、sqlplus 等）直接执行。早期方案曾考虑让容器访问宿主机的 CLI，但这带来以下问题：

- 宿主机 CLI 路径、版本、动态库依赖无法标准化
- 无法通过 Image 分发实现"开箱即用"
- 每次客户现场都需要手工部署依赖

**正确的做法**：将 CLI 工具打包进 Image，容器本身即是完整的执行环境。凭据和连接配置在运行时通过 Volume 注入，保持敏感信息与 Image 隔离。

### 1.3 为什么 Skills 不打包进 Image

Skills 是业务运维知识的载体，更新频率远高于软件版本。如果 Skills 打包进 Image：

- 每次迭代 Skill 都要重新发布 Image
- 已经在运行的所有容器都要升级
- 不同客户现场的 Skills 版本管理复杂

**正确的做法**：Skills 作为独立的共享卷挂载，所有 DeerFlow 容器实例只读挂载同一目录。修改 Skills 文件后，**无需重启任何容器**，下一个任务自动使用最新定义。

---

## 二、部署架构

### 2.1 整体架构图

```
┌─────────────────────────────────────────────────────────┐
│                   客户 Linux Server                      │
│                                                         │
│  ┌───────────────────────────────────────────────────┐  │
│  │  Nginx（统一入口，负载均衡至所有 DeerFlow 实例）    │  │
│  └────────────────────┬──────────────────────────────┘  │
│                       │                                 │
│     ┌─────────────────┼─────────────────┐               │
│     ▼                 ▼                 ▼               │
│  ┌──────┐          ┌──────┐         ┌──────┐            │
│  │DF 实例│          │DF 实例│         │DF 实例│  × N      │
│  │  #1  │          │  #2  │         │  #N  │            │
│  └──────┘          └──────┘         └──────┘            │
│  （Image 内含 psql/sqlplus/gsql/yasql 等所有 CLI）       │
│     │                                   │               │
│     └──────────────┬────────────────────┘               │
│                    │                                     │
│  ┌─────────────────▼─────────────────────────────────┐  │
│  │  共享挂载卷                                         │  │
│  │  ├── skills/        （所有实例只读，ops 直接编辑）  │  │
│  │  ├── agents/        （所有实例只读，Agent 定义）    │  │
│  │  ├── credentials/   （所有实例只读，客户填写）      │  │
│  │  └── user-data/     （读写，含 workspace/outputs/）│  │
│  └───────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────┘
              │  CLI 直连 / SSH 隧道
              ▼
┌──────────────────────────────────────┐
│  远端数据库服务器群                   │
│  Oracle / PostgreSQL / MySQL /       │
│  openGauss / YashanDB ...            │
└──────────────────────────────────────┘
```

### 2.2 容器内的执行路径

由于 CLI 已经打包在 Image 里，Agent 的执行路径完全在容器内完成：

```
Agent 接到任务
  → 读取 /mnt/skills/<category>/<db-type>/<skill-name>/SKILL.md
  → 在容器内 shell 调用 psql / sqlplus / gsql 等 CLI
  → CLI 使用 /mnt/credentials/ 中的凭据文件访问远端数据库
  → 结果写入 /mnt/user-data/outputs/（持久化到宿主机）
  → 返回给用户
```

**不需要 SSH 回宿主机，不需要任何出容器的特殊权限。**

---

## 三、Image 打包

### 3.1 Image 包含的内容

Image 是产品的"不变部分"，打包后随版本号发布：

**DeerFlow 全套服务：**
- LangGraph Server（Agent 运行时）
- Gateway API
- Frontend（Web UI）
- Nginx

**数据库 CLI 工具（用于各种数据库运维操作）：**
- `psql`（PostgreSQL 客户端）
- `sqlplus`（Oracle Instant Client）
- `sqlcl`（Oracle SQL Developer Command Line）
- `exp`, `imp`, `expdp`, `impdp`（Oracle Export/Import）
- `mysql`（MySQL 客户端）
- `mongosh`（MongoDB Shell）
- `gsql`（openGauss 客户端）
- `yasql`（YashanDB 客户端）
- `redis-cli`（如需）
- `openssh-client`（用于 SSH 到远端）

**CLI 运行时环境：**
- Oracle Instant Client 动态库
- 预设 `ORACLE_HOME`、`LD_LIBRARY_PATH`、`TNS_ADMIN` 等环境变量
- 其他数据库客户端所需的运行时库

### 3.2 Image 不包含的内容

以下内容**绝不进入 Image**，在运行时挂载注入：

- Skills 目录（业务运维知识，频繁迭代）
- Custom Agent 定义（`agents/` 目录）
- 数据库凭据和 SSH 私钥
- 数据库连接配置（tnsnames.ora、.pgpass 等）
- `config.yaml`（包含 LLM API Key）
- 运行时产生的 user-data（workspace、uploads、outputs）

### 3.3 Dockerfile 结构示例

```dockerfile
FROM deerflow-base:x.y.z

# 安装 PostgreSQL、MySQL、通用工具
RUN apt-get update && apt-get install -y \
    postgresql-client \
    mysql-client \
    openssh-client \
    && rm -rf /var/lib/apt/lists/*

# Oracle Instant Client（sqlplus + exp/imp/expdp/impdp）
COPY oracle/instantclient/ /opt/oracle/instantclient/
ENV ORACLE_HOME=/opt/oracle/instantclient
ENV LD_LIBRARY_PATH=/opt/oracle/instantclient:$LD_LIBRARY_PATH
ENV PATH=/opt/oracle/instantclient:$PATH
# TNS_ADMIN 指向运行时挂载路径，凭据不进 Image
ENV TNS_ADMIN=/mnt/credentials/oracle

# openGauss 客户端
COPY gaussdb/client/ /opt/gaussdb/client/
ENV PATH=/opt/gaussdb/client/bin:$PATH
ENV LD_LIBRARY_PATH=/opt/gaussdb/client/lib:$LD_LIBRARY_PATH

# YashanDB 客户端
COPY yashandb/client/ /opt/yashandb/client/
ENV PATH=/opt/yashandb/client/bin:$PATH

# 建立挂载点目录
RUN mkdir -p /mnt/skills /mnt/credentials /mnt/user-data /mnt/outputs
```

### 3.4 Image 命名与发布

```
示例：
  yourcompany/deerflow-dbops:1.0.0
  yourcompany/deerflow-dbops:1.1.0
  yourcompany/deerflow-dbops:latest
```

---

## 四、Skills 管理

### 4.1 Skills 的角色

Skills 是 Agent 的"运维知识库"，以 Markdown 文件描述：

- 哪类操作场景适用该 Skill
- 执行前置检查步骤
- 完整的执行流程和命令模板
- 异常处理和回滚步骤
- 报告输出格式规范

### 4.2 Skills 目录结构示例

DeerFlow 的 Skill 加载器要求一级目录必须是 `public` 或 `custom` 分类目录：

```
skills/
├── custom/                    ← 一级分类目录（custom 或 public）
│   ├── oracle/
│   │   ├── health-check/
│   │   │   └── SKILL.md      ← 数据库健康巡检
│   │   ├── fault-resolution/
│   │   │   └── SKILL.md      ← 故障排查与分析
│   │   ├── slow-sql/
│   │   │   └── SKILL.md      ← 慢 SQL 诊断
│   │   ├── session-audit/
│   │   │   └── SKILL.md      ← 会话与锁分析
│   │   └── backup-verify/
│   │       └── SKILL.md      ← 备份有效性验证
│   ├── postgresql/
│   │   ├── replication-check/
│   │   │   └── SKILL.md
│   │   └── vacuum-analysis/
│   │       └── SKILL.md
│   ├── mysql/
│   │   └── binlog-analysis/
│   │       └── SKILL.md
│   ├── opengauss/
│   │   └── health-check/
│   │       └── SKILL.md
│   └── yashandb/
│       └── health-check/
│           └── SKILL.md
└── public/                    ← 可选，用于存放公共 Skill
    └── ...
```

**重要**：`custom` 和 `public` 是一级分类目录名，不可省略。Skill 加载器通过这两个目录来区分 Skill 的来源和权限。

### 4.3 Skills 热更新机制

DeerFlow 在**每次任务执行时**读取 `/mnt/skills/` 目录，不在启动时缓存。因此：

- Ops 团队直接修改宿主机上的 `./skills/` 目录
- **无需重启容器，无需重新发布 Image**
- 下一个进来的任务即使用最新 Skill

### 4.4 Skills 版本管理建议

建议将 `skills/` 作为独立 Git 仓库管理：

```
skills/                   ← 独立 Git 仓库
├── .git/
├── custom/               ← 业务运维 Skill
│   ├── oracle/
│   ├── postgresql/
│   ├── mysql/
│   ├── opengauss/
│   └── yashandb/
└── public/               ← 可选，公共/通用 Skill
    └── ...
```

优势：变更可追踪、支持快速回滚、多人协作、与 Image 版本解耦。

---

## 五、客户现场部署

### 5.1 交付物清单

| 文件 | 说明 |
|------|------|
| `docker-compose.yaml` | 编排文件，客户不修改 |
| `nginx.conf` | Nginx 配置，客户不修改 |
| `config.example.yaml` | 配置模板，客户填写后改名 `config.yaml` |
| `README.md` | 现场部署说明 |
| Image（镜像仓库地址或离线包） | 客户 pull 或 `docker load` 导入 |
| `skills/`（初始版本） | 随交付物一同部署到宿主机 |
| `agents/`（初始版本） | Custom Agent 定义目录（SOUL.md） |

### 5.2 客户现场目录结构

```
部署目录/
├── docker-compose.yaml       ← 交付，不改
├── nginx.conf                ← 交付，不改
├── config.yaml               ← 客户填写 LLM API Key 等
├── skills/                   ← Ops 团队维护，随时迭代
│   ├── custom/               ← 一级分类目录（custom 或 public）
│   │   ├── oracle/
│   │   ├── postgresql/
│   │   ├── mysql/
│   │   ├── opengauss/
│   │   └── yashandb/
│   └── public/               ← 可选，公共 Skill
├── agents/                   ← Custom Agent 定义（SOUL.md）
│   ├── oracle-dba/
│   │   ├── config.yaml
│   │   └── SOUL.md
│   ├── postgres-dba/
│   │   ├── config.yaml
│   │   └── SOUL.md
│   └── ...
├── credentials/
│   ├── ssh/
│   │   ├── id_rsa            ← SSH 私钥（权限 600）
│   │   └── config            ← SSH 主机配置
│   ├── oracle/
│   │   ├── wallet/           ← Oracle Wallet 目录
│   │   └── tnsnames.ora      ← TNS 配置
│   ├── gaussdb/
│   │   └── .gsql_pass        ← openGauss 密码文件
│   └── .pgpass               ← PostgreSQL 密码文件（权限 600）
└── data/                     ← 自动创建
    └── user-data/            ← 挂载为 /mnt/user-data
        ├── workspace/        ← Agent 运行时工作目录
        ├── uploads/          ← 文件上传目录
        └── outputs/          ← 巡检报告等输出
```

**目录结构说明**：
- `skills/` 下一级必须是 `custom` 或 `public` 分类目录
- `data/user-data/` 作为整体挂载到容器的 `/mnt/user-data`，其下包含 `workspace/`、`uploads/`、`outputs/` 三个子目录

### 5.3 docker-compose.yaml 示例

```yaml
services:
  nginx:
    image: nginx:alpine
    ports:
      - "2026:2026"
    volumes:
      - ./nginx.conf:/etc/nginx/nginx.conf:ro
    depends_on:
      - deer-flow

  deer-flow:
    image: yourcompany/deerflow-dbops:1.0.0
    deploy:
      replicas: 5                            # 按负载调整
    volumes:
      - ./config.yaml:/config.yaml:ro
      - ./skills:/mnt/skills:ro              # 共享 Skills，只读
      - ./agents:/mnt/agents:ro              # Custom Agent 定义，只读
      - ./credentials:/mnt/credentials:ro    # 共享凭据，只读
      - ./data/user-data:/mnt/user-data      # 用户数据根目录，读写
```

**挂载说明**：
- `./data/user-data` 挂载到 `/mnt/user-data`，容器内部会自动使用其子目录 `workspace/`、`uploads/`、`outputs/`
- `./agents` 是 Custom Agent 配置目录，包含各 Agent 的 `config.yaml` 和 `SOUL.md`

### 5.4 Nginx 负载均衡配置示例

```nginx
# LangGraph Server（WebSocket 支持）
upstream langgraph_backend {
    server deer-flow:2024;
}

# Gateway API（REST API）
upstream gateway_backend {
    server deer-flow:8001;
}

server {
    listen 2026;

    # LangGraph Server - WebSocket 连接
    location /api/langgraph/ {
        proxy_pass http://langgraph_backend;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }

    # Gateway API - 鉴权、Agent 列表等
    location /api/ {
        proxy_pass http://gateway_backend;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }

    # 前端静态资源和其他请求
    location / {
        proxy_pass http://langgraph_backend;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

**端口说明**：
- `2024`：LangGraph Server 端口，处理对话流和 WebSocket 连接
- `8001`：Gateway API 端口，处理鉴权、Agent 列表获取等管理接口

---

## 六、启动与运维操作

### 6.1 首次启动

```
1. 准备 credentials/ 目录下的所有凭据文件
2. 按模板填写 config.yaml（至少填写 LLM API Key）
3. 确认 skills/ 目录已部署初始版本
4. 执行 docker compose up -d
5. 访问 http://<server-ip>:2026 验证 Web UI 正常
```

### 6.2 扩缩容

修改 `docker-compose.yaml` 中的 `replicas` 值后：

```
docker compose up -d
```

不影响正在执行的任务，Nginx 自动更新上游节点列表。

### 6.3 Skills 更新

```
1. Ops 团队修改或新增 skills/ 目录下的 SKILL.md
2. （可选）git commit 记录变更
3. 无需任何容器操作
4. 下一个新任务自动使用最新 Skill
```

### 6.4 Image 版本升级

```
1. 构建并发布新 Image（yourcompany/deerflow-dbops:x.y.z）
2. 修改 docker-compose.yaml 中的 image 版本号
3. 执行 docker compose up -d
4. Docker 依次替换旧容器，skills/credentials/user-data 挂载不受影响
```

### 6.5 查看运维报告

所有 Skill 执行产生的报告统一写入 `./data/user-data/outputs/` 目录：

```
data/user-data/outputs/
└── <thread-id>/
    ├── oracle-health-check-2026-03-30.md
    ├── fault-resolution-2026-03-30.md
    └── ...
```

---

## 七、Agent 多层编排架构

### 7.1 DeerFlow 中的三种 Agent 概念

| 类型 | 定义位置 | 角色 |
|------|---------|------|
| **Lead Agent** | `langgraph.json` + 代码 | 所有用户请求的唯一入口，LangGraph 注册的唯一图 |
| **Custom Agent** | `agents/<name>/config.yaml` + `SOUL.md` | Lead Agent 的"角色变体"，相同的图，不同的模型/工具/人格 |
| **Subagent** | Python 内置定义 | Lead Agent 动态派生的子工作者，用于并行/隔离任务执行 |

### 7.2 Custom Agent：角色扮演，不是新图

Custom Agent **不是一个独立的 LangGraph 图**，而是 Lead Agent 在运行时加载不同配置后的实例化结果。每个 Custom Agent 对应一个目录：

```
.deer-flow/agents/
└── oracle-dba/
    ├── config.yaml    ← 指定专属模型、工具组（可选）
    └── SOUL.md        ← Oracle DBA 的角色定义、行为约束、输出规范
```

`config.yaml` 示例：

```yaml
name: oracle-dba
description: "Oracle 数据库专属运维 Agent"
model: claude-3-7-sonnet   # 可选，覆盖全局模型配置
tool_groups:               # 可选，限制只能使用的工具组
  - sandbox
  - community
```

用户在 UI 中选择对应 Agent 后，请求会携带 `agent_name` 参数，系统加载该 Agent 的 SOUL.md 注入 system prompt，并按 config.yaml 中的配置调整工具集和模型。**当前不支持系统按对话内容自动路由，需用户显式选择。**

### 7.3 Subagent：并行执行，上下文隔离

Subagent 由 Lead Agent 在执行任务时通过 `task` 工具动态派生，核心能力：

- 并行执行多个独立子任务（默认最多 3 个并发）
- 将执行过程与主对话隔离，避免长命令输出污染上下文
- Skills 内容在 Subagent 创建时**自动注入**其 system prompt

当前内置两种 Subagent 类型：

| 类型 | 适用场景 | 可用工具 |
|------|---------|---------|
| `general-purpose` | 多步骤复杂任务，需要推理+执行 | 所有工具 |
| `bash` | 专注 shell 命令执行，输出冗长 | bash、文件读写等受限集合 |

**重要约束：Subagent 无法再派生 Subagent**（架构硬限制），也无法调用其他 Custom Agent。

### 7.4 面向数据库运维的多层编排模式

#### 以 Oracle 故障排查为例

```
用户（选择 oracle-dba agent）发出请求
  ↓
Oracle Custom Agent（Lead Agent + Oracle SOUL.md 加载）
  ├── 识别为故障排查任务，读取 custom/oracle/fault-resolution/SKILL.md
  │
  ├── 并行派生 Subagents（信息收集阶段）：
  │     ├── Subagent-1（general-purpose）
  │     │   执行诊断 SQL，收集 AWR/ASH 报告
  │     │   调用 sqlplus → 查询 v$session、v$sql_area
  │     │
  │     └── Subagent-2（general-purpose）
  │         分析告警日志、跟踪文件
  │         调用 sqlplus → 查询 dba_alert_history
  │
  ↓（等待所有 Subagent 完成，Lead Agent 汇总结果）
  │
Oracle Custom Agent 执行故障分析（LLM 推理汇总）
  ↓
生成结构化故障报告 → 写入 /mnt/user-data/outputs/<thread-id>/
  ↓
回复用户，附报告摘要
```

#### 各层可行性对照

| 期望层次 | DeerFlow 实现方式 | 可行性 |
|---------|-----------------|--------|
| 按数据库类型路由到专属 Agent | 用户显式选择 Custom Agent | ⚠️ 需手动选择 |
| Oracle Agent 专业角色行为 | Oracle SOUL.md 定义 | ✅ |
| 并行信息收集 Subagents | `task` 工具，general-purpose，Skills 自动注入 | ✅ |
| 独立的故障分析 Agent 层 | Lead Agent 自身承担（汇总 → 推理分析） | ✅（合并层次） |
| 生成报告并回复用户 | Lead Agent 写 /mnt/user-data/outputs/ 后返回 | ✅ |
| Subagent 向另一 Agent 传递结果 | 不支持 | ❌ 架构硬限制 |

#### 独立分析 Agent 的扩展路径

若希望"故障分析"由一个独立的专属 Agent 完成（而非 Lead Agent 兼任），可通过 **ACP（Agent Context Protocol）** 集成：

- 将故障分析 Agent 实现为独立的 ACP 兼容进程（例如基于 deepagents）
- 在 `config.yaml` 的 `acp_agents` 中注册该外部 Agent
- Lead Agent 通过 `invoke_acp_agent` 工具将汇总数据传递给它，获取分析结论

### 7.5 按数据库类型规划 Custom Agent

```
agents/                   ← 宿主机目录，挂载到容器 /mnt/agents
├── oracle-dba/
│   ├── config.yaml       ← Oracle 专属模型、工具组
│   └── SOUL.md           ← Oracle DBA 角色：命令习惯、故障模式、操作约束
├── postgres-dba/
│   ├── config.yaml
│   └── SOUL.md
├── mysql-dba/
│   ├── config.yaml
│   └── SOUL.md
├── opengauss-dba/
│   ├── config.yaml
│   └── SOUL.md
└── yashandb-dba/
    ├── config.yaml
    └── SOUL.md
```

**注意**：Custom Agent 定义在宿主机 `agents/` 目录下，通过 `- ./agents:/mnt/agents:ro` 挂载到容器内。容器内通过 `Paths.agents_dir` 加载这些配置。

每个 Custom Agent 的 SOUL.md 应定义：

- 该数据库的核心运维知识（专用命令、常见故障模式）
- 何时调用哪类 Skill
- 操作风险等级判断标准（只读 / 受控变更 / 高危）
- 报告输出格式规范（故障报告、巡检报告等）

---

## 八、安全规范

### 8.1 凭据安全

- 私钥文件权限必须为 `600`，目录权限为 `700`
- `.pgpass`、`.gsql_pass` 等密码文件权限必须为 `600`
- 凭据目录统一以只读（`:ro`）方式挂载进容器
- **LLM 不直接接触明文凭据**——CLI 工具通过环境变量或文件路径自动读取，Agent 看到的只是操作结果

### 8.2 Skill 操作安全分级

| 风险等级 | 操作示例 | 执行策略 |
|---------|---------|---------|
| 只读诊断 | 健康巡检、慢 SQL 分析、会话查询 | 自动执行，无需确认 |
| 受控变更 | 参数调整、索引建议、权限检查 | 需人工确认后执行 |
| 高危操作 | Kill Session、主备切换、账号变更 | 必须双重确认 + 审计记录 |

### 8.3 审计建议

- 结合 Docker 日志收集（如 Loki / ELK）统一采集所有容器日志
- 每次 Skill 执行产生结构化日志，包含：任务 ID、Skill 名称、执行命令、返回码、执行时间
- 高危操作写入独立审计日志，与运行日志分开存储

---

## 九、未来扩展到 Kubernetes

当单机无法满足规模需求时，现有设计可平滑迁移到 K8s：

| 当前（Docker Compose） | K8s 对应 |
|------------------------|----------|
| `deploy.replicas: N` | `Deployment.spec.replicas: N` |
| skills bind mount | `ReadWriteMany PVC`（NFS/CephFS）或 `ConfigMap` |
| credentials volume | `Kubernetes Secret` + volume mount |
| workspace / reports volume | `ReadWriteMany PVC` |
| Nginx | `Service` + `Ingress Controller` |
| Image 版本管理 | `Helm Chart` + `values.yaml` |

---

## 十、总结

| 问题 | 解决方式 |
|------|---------|
| 如何分发产品 | Image + docker-compose.yaml + README |
| CLI 工具如何管理 | 打包进 Image，随软件版本发布 |
| Skills 如何快速迭代 | 独立 Git 仓库，挂载为共享卷，热更新无需重启 |
| 凭据如何管理 | 客户现场准备，只读 Volume 挂载，不进 Image |
| 如何水平扩展 | 调整 replicas，Nginx 自动负载均衡 |
| 运行时报告如何持久化 | 写入共享 reports volume，汇聚到宿主机 |
| 如何升级版本 | 更新 Image tag，docker compose up -d |
| 如何定制专属数据库 Agent | 创建 Custom Agent 目录，编写 SOUL.md |
| 如何并行执行多步骤运维任务 | Lead Agent 通过 task 工具派生 Subagents |
| Subagent 能否再调用其他 Agent | 不支持，Lead Agent 兼任分析汇总角色 |
