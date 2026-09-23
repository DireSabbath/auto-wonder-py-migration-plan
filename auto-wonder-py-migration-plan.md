# AutoWonder Java → Python 完整 1:1 迁移方案(一次性最终版)

> 版本:v3.0(2026-09-23,定稿)
> 范围:`ai-sdlc/auto-wonder`(Java 参考实现,只读)→ `ai-sdlc/auto-wonder-py`(全新 Python 实现,待建)
> 决策记录(用户逐项确认):全新并行实现 / FastAPI 全家桶 / Docker Compose 验证 / **完整 1:1 迁移,不分期、无裁剪,53 个包全部纳入;一次性交付,不排期、不分工期、不设里程碑**
> v2.0 变更:采纳外部工程评审——目标由"MVP 裁剪"改为"完整 1:1";灰色地带 8 包(insights/environment/debuglog/dashboard/category/guidance/setting/tenant,共 86 文件)全部纳入;tenant SQL 隔离机制纳入契约与映射表;原分期验收矛盾随全量覆盖消除
> v3.0 变更(定稿):删除波次/工期概念,交付形态改为一次性完整交付(§7);tenant 表数修正 35→38(以 TenantTables.java 实数);tenant 隔离定为"不弱于 Java"策略并记录有意分歧(§6.3);tests/tenant/ 增补写路径断言;PlatformAdminController 归入 platform/;终验收补机制专项与前端功能闭环

---

## 1. 背景与目标

AutoWonder 是多智能体软件交付平台:用户创建工单(需求/缺陷/任务),指派给按角色组织的 AI 数字员工小队(7 角色:FS_DEV 开发、CR 代码评审、QA 测试、REQ_CLARIFIER 需求澄清、PROJECT_MANAGER、CONFLICT_RESOLVER、DBA),服务端将 SDLC 各步骤调度到自托管执行器(npm 客户端 `autowonder`,Qoder CLI 运行时)执行。

**目标:完整 1:1 迁移**——Java 侧 53 个顶层包 / 1,012 个主源文件 / 481 个测试文件全部覆盖,无任何域延后或裁剪。Java 代码一行不改、继续可运行,作为黄金参考(golden master);Python 版逐域实现并与 Java 双栈对拍,最终以"现有 React 前端全部页面可用 + 现有执行器协议全量兼容 + 全 API 面等价"为验收。

### 1.1 "1:1" 的语义(契约一致,实现等价)

| 层面 | 承诺 |
|---|---|
| **逐字段/字节级一致(契约层)** | 84 表 schema;WS 协议 §5 全部帧;JWT claim 与密钥;bcrypt `$2a$`;AESGCM 密文格式;Result 信封与 ErrorCode 全量;API 路径/参数/camelCase 字段/错误语义;**tenant 隔离行为(38 表 SELECT 注入 `tenant_id`;py 侧执行"不弱于 Java"策略,见 §6.3)**;18 个定时任务的行为与集群锁语义;Aone 开关(社区版默认 false,代码路径完整保留) |
| **等价重组(实现层)** | MyBatis XML → SQLAlchemy 2.0;Tomcat 线程池 → asyncio;文件结构按 Python 域包四件套重组(非逐文件映射);Java 481 个测试文件 → 等价 pytest 覆盖(逐场景而非逐文件);Java e2e-tests/ 是 Java 发布门禁,不迁移,py 自建跨平台 harness |

## 2. 现状盘点(Java 侧,只读)

### 2.1 规模

| 维度 | 数据 |
|---|---|
| 主源码 | 1,012 个 Java 文件,53 个顶层包(Spring Boot 2.7.18 / Java 21 / Maven) |
| 测试 | 481 个文件(Testcontainers-MySQL + H2 兼容门禁) |
| 数据库 | MySQL 8,84 张表(`docs/autowonder-schema.sql`,V070 基线;迁移 V036–V070) |
| ORM | MyBatis,78 个 XML mapper |
| 端口 | 7001(单体 jar,前端静态资源打入) |

### 2.2 域清单(53 包全量,无裁剪;分组仅为清单组织,非实施批次)

| 分组 | 包(文件数) | 说明 |
|---|---|---|
| 基座 | common(8)/util(9)/configuration(10)/redis(4)/filter(2)/controller(2)/context(2)/model(1)/security(3)/log(15)/storage(10)/access(12)/audit(8) | 横切设施、Result 信封、鉴权中间件、日志、对象存储抽象 |
| 身份 | auth(6)/user(23)/workspace(41,映射 org 表)/branding(7)/setting(7) | JWT、用户、工作空间、平台品牌、系统设置 |
| 配置数据 | agent(36)/squad(12)/sdlc(15)/statemachine(19)/skill(15)/memory(17)/repo(18)/environment(13)/category(12)/template(7)/guidance(8)/taskpackage(8) | 智能体/小队/SDLC/状态机/技能/记忆/代码库/环境变量/资产分类/模板/指引/任务包 |
| 工单与记录 | workitem(49)/clarification(6)/notification(21)/debuglog(11)/aiusage(19)/insights(20)/dashboard(12) | 工单全生命周期、澄清、通知、调试日志、AI 用量、洞察、工作台仪表盘 |
| 存储与 AI | artifact(14)/ai(24,CliExecutor 子进程) | 制品/需求文档、服务端 AI 会话 |
| 执行与调度 | executor(37)/websocket(37)/dispatch(57) | 执行器注册、双 WS 端点、调度派发主环+checkpoint 恢复 |
| 协作与外围 | conversation(50)/scheduledtask(51)/mcp(20)/im(36)/integration(116,Aone 默认禁用仍完整实现)/evolution(76)/backup(3) | 会话域、定时任务、MCP 服务、IM、Aone/外部集成(outbox)、资产进化、备份 |

> 另:tenant(3)不作域包,以隔离机制纳入 `db/tenant.py`(见 §6.3);合计 53 包 / 1,012 文件,与 §2.1 一致。

### 2.3 关键机制

- **鉴权**:自定义 JWT AuthFilter(access 2h / refresh 7d,jjwt;claim `uid`/`workspace`/`jti`),无 Spring Security 过滤器链;`@RequireWorkspaceAccess` AOP 切面控工作区访问
- **tenant 隔离(全局透明)**:`tenant/TenantInterceptor.java` 带 `@Component` 自动注册,MyBatis StatementHandler 拦截——当上下文有 workspaceId 且语句为 SELECT 时,对 `TenantTables.TABLES` 中 **38 张表**(workitem/agent/squad/sdlc/dispatch/ai_session/memory/skill/notification/ai_usage/system_setting 等,完整清单见该文件)的 SQL 经 jsqlparser 注入 `AND tenant_id = <workspaceId>`;注意 Java 改写仅覆盖 **FROM 单表的 PlainSelect**(UNION/FROM 子查询/JOIN 附加表不注入,jsqlparser 解析异常静默放行原 SQL)。**这是所有工作区隔离的真实执行点**,Python 侧按"不弱于 Java"策略实现(见 §6/§9)
- **实时**:javax.websocket 双端点(浏览器 `/ws` + 执行器 `/ws/executor`);执行器侧协议由 `docs/scheduler-executor-protocol.md` 定义(帧目录/幂等/状态机/TTL)
- **定时**:`@Scheduled` ×18,全部带 Redis/DB 集群锁(补偿/恢复/轮询/清理)
- **AI 执行**:服务端不直调交付 LLM——执行器跑本地 CLI;服务端 AI 功能由 `CliExecutor` 子进程调 Claude Code CLI(ANTHROPIC_* env)
- **存储**:对象存储强制(OSS 或 S3/MinIO,无内存回退),4 桶:任务包/制品/技能/备份
- **API 契约**:`docs/openapi-reference.md`(98 端点)只是**文档化子集**;1:1 的真实契约面 = 全部 Controller 实际暴露的端点(以源码为准提取清单)

### 2.4 运行/验证现状

- 本地栈:MySQL 8(33060)+ Redis(63790)+ **MinIO(对象存储强制)**,`docker-compose.dependencies.yml` 只提供前两者
- e2e:`e2e-tests/verify.sh` 是 Java 发布门禁(build→image→compose→smoke→authchain→logscan),**仅支持 macOS/CentOS**,Windows 不可用;属于 Java 侧设施,不迁移
- 种子:`docs/autowonder-community-templates.sql`(4 套系统小队模板,含 7 角色全链)

## 3. 硬约束

1. **同步子树纪律(最高优先级)**:`ai-sdlc/auto-wonder/` 被社区同步 `git archive` + `rsync --delete` 整目录替换,规则明确禁止镜像子树内 GitHub-only 修改。⇒ **绝不写入该子树**(含 e2e-tests/);新工程放兄弟目录 `ai-sdlc/auto-wonder-py/`;所有引用资产由 `backend/scripts/sync_from_java.py` 单向拷出。
2. **Schema 即契约**:复用 `autowonder-schema.sql`(84 表 V070),SQLAlchemy 映射现有表,不重设计;Alembic 仅 stamp 不产 DDL;V071+ 随同步到达时补模型重新 stamp。
3. **协议即契约**:`scheduler-executor-protocol.md` §5 帧目录逐字段实现(camelCase 字段、幂等语义、状态机转换、TTL),保证现有 npm 执行器客户端零改动接入 Python 服务端。
4. **跨平台**:Windows 11 + Git Bash + Docker Desktop;所有新工具链 Python 优先,禁 POSIX-only 依赖(吸取 verify.sh 教训)。

## 4. 技术栈选型

| 决策 | 选择 | 理由 |
|---|---|---|
| Python | 3.12(uv 自管) | 本机无可用 Python(仅 WindowsApps 存根);wheel 覆盖最全 |
| 包管理 | uv + uv.lock | Windows 原生单二进制、可复现、快 |
| Web | FastAPI ~0.115 + uvicorn[standard] ~0.32 | 既定选型;异步支持契合长连接场景 |
| ORM | SQLAlchemy 2.0 async + asyncmy | 执行器长连接/实时推送需全异步;**禁用 SQLite 测试**(JSON 列/DATETIME(3) 方言差异),测试连真 MySQL |
| 迁移 | Alembic(仅 stamp) | py 侧永不产 DDL,schema 唯一来源是 Java 侧 |
| Redis | redis-py ~5.2(redis.asyncio) | 对应 Jedis |
| JWT | PyJWT(HS256) | claim 名对齐 jjwt,同 secret 双栈令牌互认 |
| 口令 | bcrypt | 与 Spring BCrypt `$2a$` 双向互通 |
| 加密 | cryptography(AESGCM) | 字节级移植 `AesGcmSecretCrypto.java`(executor token_ref 双栈可解) |
| 存储 | aioboto3(S3 协议,MinIO 兼容)+ oss2 | MinIO 为本地验证栈;OSS 原生后端同样实现(1:1) |
| 定时 | APScheduler(AsyncIOScheduler) | 18 个 @Scheduled 全量等价 + Redis 锁 |
| 质量 | ruff + mypy + pytest + pytest-asyncio + import-linter | 全跨平台;import-linter 强制域边界 |

## 5. 最终 Python 工程目录(`ai-sdlc/auto-wonder-py/`,完整)

### 5.1 顶层结构:frontend/ + backend/ 双子目录

标准全栈 monorepo 布局,产品级容器:

```
auto-wonder-py/                  # 仓库目录(带 -py 仅为与 Java 树共存,不构成产品名)
├── README.md / .gitignore / .gitattributes
├── frontend/                    # 前端:Java 场单向同步拷贝(源码+dist),禁止手改
│   ├── src/ …                   # React 源码(只读镜像)
│   ├── dist/                    # 构建产物(由 backend 静态服务)
│   └── vite.py-backend.config.ts# HMR 开发配置:代理 /api、/ws → :7002
└── backend/                     # 后端 = 完整独立 Python 工程(详见 5.3)
    ├── pyproject.toml / uv.lock / Dockerfile
    ├── alembic/  src/autowonder/  tests/  verify/  scripts/
```

**前端适配纪律(2 条)**:
1. **frontend/ 是单向同步拷贝,不是 fork**:前端源码唯一真源在同步子树 `ai-sdlc/auto-wonder/frontend/`(社区同步更新它)。`sync_from_java.py` 把**源码+dist 一起**单向同步过来;仓库守卫禁止手改 `frontend/`——手改必然与真源漂移。Java 版退役、前端归 Python 项目所有时再解除该纪律。
2. **HMR 开发**:原 `frontend/vite.config.ts` 代理硬编码 `:7001` 且不可改(子树纪律);用 `frontend/vite.py-backend.config.ts`(root 指 `frontend/`,`/api` 与 `/ws` 代理到 `:7002`)以 `npx vite --config` 启动——Java 树零改动。

### 5.2 组织原则(Python 风格)

1. **src 布局**:包在 `src/autowonder/` 下,避免误导入未安装包,构建即验证真实安装形态。
2. **域为中心的模块化单体(modular monolith)**:每个业务域一个自包含包,内含 `router.py`(端点)/ `service.py`(业务)/ `models.py`(SQLAlchemy)/ `schemas.py`(Pydantic)——**不搞全局 controller/service/dao 三层大目录**。
3. **命名**:包/模块 snake_case 复数资源名(`workitems`、`agents`);类名对齐 Java 概念(`DispatchService` → `dispatch/service.py`),API 字段名保持 camelCase 对齐前端契约,Python 内部一律 snake_case(Pydantic alias 转换)。
4. **配置集中**:ruff / mypy / pytest 配置全部收在 `pyproject.toml`;依赖只经 `uv.lock` 锁定。
5. **横切与域分离**:`core/`、`db/`、`security/`、`api/` 是横切设施;业务域各建包(与 Java 53 包一一对应,见 5.3)。

**为什么以包形式组织**(而非散置脚本目录):千文件规模的企业应用必须有安装/构建/分发边界——代码要打进 Docker 镜像、以安装形态运行(`autowonder-serve` 入口)、被 pytest 以同样形态导入;包是 Python 组织此类代码的标准形态,与 Java 的 `com.aliyun.autowonder.*` 包 + Maven 一一对应(`src/autowonder/dispatch/` ≈ `com.aliyun.autowonder.dispatch`);ruff/mypy/pytest 配置、console scripts、uv.lock 的锚点都是包。

**为什么单包(`src/autowonder/`)而非多包/monorepo**:
- **对齐被迁移物形态**:Java 侧本就是单 jar 单体;一包 = 一个部署单元 / 一份 uv.lock / 一个 Alembic 环境 / 一个 Dockerfile,精确对应"单 JVM"。
- **域边界靠 import 纪律,不靠打包边界**:域间只允许经对方 `service.py` 门面调用,用 **import-linter** 契约在 CI 强制。
- **84 张表跨域外键需要统一模型注册点**:SQLAlchemy metadata 要一个聚合注册点(`db/base.py`)。
- **将来可拆**:域包四件套 + service 门面就是未来拆分线。

### 5.3 后端完整目录树(53 包一一对应)

```
backend/                          # auto-wonder-py/backend/,完整独立 Python 工程
├── pyproject.toml                # 项目名 auto-wonder(不带 -py);依赖+工具配置+scripts
├── uv.lock / .env.example / .gitignore / .gitattributes
├── Dockerfile                    # 多阶段: uv sync --frozen → python-slim 运行
├── alembic.ini
├── alembic/
│   ├── env.py                    # async engine 接入
│   └── versions/py0001_v070_baseline.py   # 仅 stamp,无 DDL
│
├── src/
│   └── autowonder/               # ← 唯一 Python 包(src 布局)
│       ├── __init__.py           # __version__
│       ├── main.py               # create_app() 工厂
│       ├── settings.py           # pydantic-settings:读 Java 同名 env+JDBC URL 解析
│       │
│       ├── core/                 # ── 横切(≈Java common/configuration/context/filter/redis/log)──
│       │   ├── result.py         # Result 信封 + ErrorCode 全量移植(≈common/result)
│       │   ├── errors.py         # BizException 及异常→HTTP/错误码映射
│       │   ├── logging.py        # JSON 日志 + request_id(≈log/BizLogger+MDC)
│       │   ├── context.py        # contextvars 请求/工作区上下文(≈context/AutoWonderContext)
│       │   ├── clock.py          # naive 本地时间单点(防 8h 漂移)
│       │   ├── redis.py          # redis.asyncio 客户端单例(≈redis/)
│       │   ├── locks.py          # 分布式锁 SET NX PX(≈RedisLockRegistry)
│       │   ├── events.py         # 域事件总线(事务 AFTER_COMMIT 派发)
│       │   └── tasks.py          # 异步池/to_thread 包装(≈ThreadPoolManager)
│       ├── db/
│       │   ├── base.py           # DeclarativeBase + 软删/乐观锁 mixin;聚合各域模型
│       │   ├── session.py        # async_sessionmaker + get_session 依赖
│       │   └── tenant.py         # ⭐tenant 隔离:with_loader_criteria 全局注入(见 §6)
│       ├── security/             # password(bcrypt $2a$)/jwt(PyJWT)/crypto(AESGCM 字节级)
│       ├── api/
│       │   ├── middleware.py     # AuthMiddleware,白名单逐条照抄 AuthFilter
│       │   ├── deps.py           # get_current_user / require_access(≈access/ 切面)
│       │   ├── errors.py         # 全局异常处理器 → Result 信封
│       │   └── spa.py            # frontend/dist 静态服务 + SPA fallback(≈WebMvcConfig)
│       │
│       ├── platform/             # 品牌/平台信息/平台管理员(≈branding + access/PlatformAdminController)
│       ├── auth/  users/         # 认证(≈auth)/用户(≈user)
│       ├── workspaces/           # 工作空间(≈workspace,映射 org/org_member/org_invite)
│       ├── agents/  squads/      # 智能体(≈agent)/小队(≈squad)
│       ├── sdlcs/  statemachines/# SDLC 定义(≈sdlc)/状态机模板(≈statemachine)
│       ├── skills/  memories/  repos/            # 技能/记忆/代码库
│       ├── environments/         # 环境变量(≈environment,前端 AgentEditPage 依赖)
│       ├── categories/           # 资产分类(≈category)
│       ├── templates/            # 资产模板(≈template)
│       ├── guidance/             # 指引(≈guidance)
│       ├── settings/             # 系统设置(≈setting,tenant 表 system_setting)
│       ├── taskpackages/         # 任务包装配(≈taskpackage)
│       ├── workitems/            # 工单(≈workitem:CRUD/流转/评论/事件/watcher/timeline)
│       ├── clarifications/       # 工单澄清(≈clarification)
│       ├── notifications/        # 站内通知(≈notification)
│       ├── debuglogs/            # 调试日志(≈debuglog)
│       ├── aiusage/              # AI 用量/配额(≈aiusage)
│       ├── insights/             # 洞察快照(≈insights,前端 /api/insights/*)
│       ├── dashboards/           # 工作台仪表盘(≈dashboard,/api/dashboard/realtime)
│       ├── audits/               # 审计日志(≈audit)
│       ├── storage/              # 对象存储(≈storage:s3.py + oss.py 双后端 + 4 桶)
│       ├── artifacts/            # 制品/需求文档(≈artifact)
│       ├── ai/                   # 服务端 AI 会话(≈ai:cli_executor.py=asyncio.subprocess)
│       ├── executors/            # 执行器(≈executor:注册/重启/版本/模型目录)
│       ├── ws/                   # WebSocket 层(≈websocket)
│       │   ├── executor.py       # /ws/executor?executorId&token
│       │   ├── browser.py        # /ws 浏览器端点
│       │   ├── frames.py         # 协议 §5 全部帧(camelCase 逐字段)
│       │   ├── inbound.py  presence.py
│       ├── dispatch/             # ⭐调度派发(≈dispatch)
│       │   ├── models.py  router.py  service.py
│       │   ├── selector.py  sdlc_driver.py  handoff.py
│       │   ├── packager.py  context.py  recovery.py
│       ├── conversations/        # 会话域(≈conversation:轮次/事件/澄清/共享)
│       ├── scheduledtasks/       # 定时任务(≈scheduledtask:触发/编排/恢复/补偿)
│       ├── mcp/                  # MCP 服务(≈mcp:POST /api/mcp + 令牌体系)
│       ├── im/                   # IM 提供商抽象(≈im)
│       ├── integrations/         # 外部集成(≈integration:aone[默认禁用仍全量实现]/
│       │   │                      dingtalk/feishu/outbox/回执)
│       ├── evolution/            # 资产进化(≈evolution:贝叶斯决策/提案/灰度)
│       ├── backups/              # 备份(≈backup)
│       └── jobs/                 # 18 个 @Scheduled 全量等价(APScheduler+Redis 锁)
│
├── tests/                        # 等价覆盖 Java 481 测试文件(逐场景,非逐文件)
│   ├── conftest.py               # compose 真 MySQL fixture(禁 SQLite)
│   ├── unit/  api/  ws/  parity/ # 信封/JWT/加密/帧…;ASGITransport 逐域;协议 fixtures;双栈对拍
│   └── tenant/                   # ⭐隔离专项:读路径 A/B 互访断言 + 写路径 tenant_id 落库断言
│
├── verify/                       # python -m verify,纯 Python 跨平台
│   ├── cli.py                    # up/down/logs/smoke/authchain/logscan/parity/dispatch-e2e/pages
│   ├── compose/                  # deps.yml(MySQL 33061/Redis 63791/MinIO 9000)+ app.yml(:7002)
│   ├── initdb/                   # 001-schema.sql + 002-templates.sql(sync 脚本拷出)
│   ├── harness/  mock_executor.py
│   ├── pages/                    # Playwright 全页面走查(对齐 frontend/src/features/*)
│   └── parity/cases/*.yaml       # 以 Controller 源码提取的**全量端点清单**为基准
│
├── scripts/
│   ├── sync_from_java.py         # 单向拷贝:schema/种子/前端源码+dist/协议文档/TenantTables 清单
│   ├── bootstrap_env.py          # 生成本地 .env;--from-java-env 只读导入同密钥
│   ├── extract_endpoints.py      # 从 Java Controller 提取全量端点清单→parity 基准
│   ├── load_seed.py  check_schema_sync.py
│
└── (frontend/ 在仓库顶层,见 5.1)
```

**说明**:
- 每个域包对应一个(或一组)Java 顶层包,内部统一四件套 `models.py / router.py / service.py / schemas.py`,小域可只含其中若干件;域分组见 §2.2(仅为清单组织,非实施批次)。
- `pyproject.toml` 的 `[project.scripts]` 提供 `autowonder-serve`(生产入口);开发/验证统一 `uv run` + `python -m verify`(均在 `backend/` 下执行)。
- Dockerfile/镜像与服务名统一 `autowonder`(不带 -py)。

**命名规范(已确认)**:

| 对象 | 名称 | 说明 |
|---|---|---|
| 仓库目录 | `ai-sdlc/auto-wonder-py/` | 带 -py 仅为与 Java 树共存,不构成产品名 |
| pyproject 项目名 | `auto-wonder` | 不带 -py |
| import 包名 | `autowonder` | `src/autowonder/` |
| Docker 镜像/服务名 | `autowonder` | compose 内服务名 |

## 6. 数据库策略

1. `autowonder-schema.sql` 拷为 `verify/initdb/001-*.sql`,仅全新卷经 docker-entrypoint-initdb.d 执行(复用卷会静默跳过——Java e2e 的已知坑)。
2. 模型显式表名;行为细节必须复刻:AUTO_INCREMENT=10000 起始、软删过滤、乐观锁 version、`with_for_update()` 悲观读。
3. **tenant 隔离(核心契约)**:`db/tenant.py` 用 SQLAlchemy 2.0 `with_loader_criteria()` + `do_orm_execute` 事件,在会话上下文有 workspaceId 时对 **38 张 tenant 表**(清单以 `TenantTables.java` 为唯一真源,由 sync 脚本拷出为 fixture)自动注入 `tenant_id` 过滤。**策略 = 不弱于 Java**:Java 改写仅覆盖 FROM 单表的 PlainSelect(UNION/FROM 子查询/JOIN 附加表不注入,解析异常静默放行),py 侧 criteria 覆盖 ORM 查询全部形态——严格性只增不减;超出 Java 覆盖的部分记为**有意分歧(安全修复)**,parity 场景只选 Java 同样过滤的查询形态,不反向复刻 Java 缺口。**约束:py 侧对这 38 张表禁止绕过 ORM 的裸 SQL**;确需手写 SQL 的路径必须经统一入口补过滤;INSERT 与 Java 一致不自动注入,`tenant_id` 由业务代码显式赋值——读写双向断言见 tests/tenant/。
4. 种子:`autowonder-community-templates.sql`(幂等)作 `002-*.sql` + `scripts/load_seed.py` 幂等重放。
5. 时区/字符集:容器 `TZ=Asia/Shanghai`、`utf8mb4` + `SET time_zone='+08:00'`、DB 时间统一 naive 本地(`clock.py` 单点封装);DATETIME(3) 毫秒不丢。

## 7. 交付形态(一次性完整交付)

**不排期、不分工期、不设批次与里程碑**:53 个包、18 个定时任务、全量端点、全量等价测试是**同一份交付物**,唯一完成标准是 §11 终验收全绿。

唯一存在的顺序是**实现的技术依赖**(由实现过程自然满足,不构成阶段划分):

1. 横切设施(core/db/security/api)先于业务域——它们是所有域的运行底座;
2. 基础业务域先于 ws/dispatch——长连接与调度消费各域模型与 service 门面;
3. 外围域(conversations/scheduledtasks/mcp/im/integrations/evolution/backups)仅依赖各自上游域,可随时并行推进。

**持续验收**:任一域完成即跑该域 pytest 与 parity 用例(Java 黄金主在线即对拍),不等其他域、不设阶段门;全部就绪后进入 §11 终验收。

## 8. 验证策略

- **双栈并跑黄金主**:py 栈端口与 Java 栈错开(MySQL 33061/Redis 63791/MinIO 9000/API 7002,Java 是 33060/63790/7001),同机对拍。
- **端点清单即基准**:`scripts/extract_endpoints.py` 从 Java Controller 源码提取**全量**端点清单(路径/方法/参数),作为 parity 基准;`openapi-reference.md` 仅作交叉校验(它是子集)。
- **verify harness**(`python -m verify ...`):up/down/logs/smoke/authchain/logscan/parity/dispatch-e2e/pages,输出 JSON verdict。
- **pytest**:unit / api(httpx ASGITransport+真 MySQL)/ ws(协议 §5 fixtures 逐字段)/ parity(YAML 场景双栈规范化深比对,剔 id/时间戳/traceId/token;Java 离线自动 skip)/ **tenant/**(隔离读写路径专项)。
- **前端活验收**:全部 features 页面(agent/workitem/squad/insights/environmentVariables/evolution/scheduledTask/integration/settings…)以 Playwright 逐页走查——1:1 目标下**页面清单 = 前端路由全集**,不允许 404。
- **执行器一致性**:`mock_executor.py`(可注入故障:不 ACK/超时/重复帧/断线重连)+ 有 Qoder 环境时真实 npm 客户端。

## 9. Java → Python 机制映射(要点)

| Java | Python |
|---|---|
| AuthFilter(OncePerRequestFilter) | ASGI 中间件,白名单正则照抄 |
| @RequireWorkspaceAccess / CapabilityAspect | FastAPI Depends(`require_access`) |
| jjwt(含 dispatch token purpose/subjectId) | PyJWT 同 claim 同 secret |
| **TenantInterceptor + TenantSqlRewriter(38 表 SELECT 注入,仅单表 PlainSelect)** | **`db/tenant.py`:with_loader_criteria + do_orm_execute,覆盖 ORM 全形态(不弱于 Java);38 表禁裸 SQL,tests/tenant/ 读写路径专项** |
| MyBatis 78 XML mapper + jsqlparser | SQLAlchemy 2.0 声明模型 + with_for_update(jsqlparser 不再需要——ORM 层原生注入) |
| Jedis + RedisLockRegistry | redis.asyncio + SET NX PX 自封装 |
| JSR-356 WS + SessionRegistry + NodeMailboxListener | FastAPI WebSocket + 会话表 + Redis pub/sub `node:dispatch:broadcast` |
| @Scheduled ×18 + 集群锁 | APScheduler AsyncIOScheduler + locks.py |
| @EnableAsync 线程池 | asyncio.create_task + Semaphore;bcrypt 等阻塞走 anyio.to_thread |
| ApplicationEvent AFTER_COMMIT | 域事件总线(事务提交回调后派发) |
| CliExecutor 子进程 | asyncio.subprocess(ANTHROPIC_* env 透传照抄) |
| OkHttp(Aone/DingTalk/Feishu OpenAPI) | httpx(签名/限流逐字段照抄) |
| IntegrationOutbox/AoneOutboxDispatcher | 同模式:SQLAlchemy 表 + jobs 轮询投递 |
| Tomcat 2000 线程 | 事件循环 + asyncmy 池;uvicorn 多 worker 预留 |
| Result T / BizLog + MDC | `core/result.py` / contextvars + JSON logging |
| 静态资源 SPA fallback(WebMvcConfig) | StaticFiles + catch-all index.html |
| e2e verify.sh / authchain.sh / logscan.sh | verify harness(Python 跨平台重写) |

## 10. 风险与缓解

| # | 风险 | 缓解 |
|---|---|---|
| 1 | 同步子树被污染 | 单向拷贝脚本 + CI 守卫(git diff 禁触 `ai-sdlc/auto-wonder/` 与 `frontend/`) |
| 2 | schema 漂移(V071+ 随同步到达) | `check_schema_sync.py`(information_schema vs 模型元数据)CI 门禁;补模型重新 stamp |
| 3 | **tenant 隔离语义差异**(Java 仅改写单表 PlainSelect;py 的 ORM criteria 不覆盖裸 SQL) | 38 表禁裸 SQL 约定 + 统一入口 + tests/tenant/ 读写路径专项;TenantTables 清单由 sync 脚本固定为 fixture;parity 场景只选 Java 同样过滤的查询形态,py 更严处记为有意分歧(安全修复) |
| 4 | WS 协议不兼容现有执行器 | 协议 §5 fixtures 逐字段断言;mock_executor 先行;协议文档随 sync 更新时 review diff |
| 5 | 事件循环 vs 线程池模型差异 | 阻塞调用走 to_thread;p95 双栈对比 |
| 6 | MySQL 时区/字符集暗坑 | TZ/utf8mb4/naive 本地时间单点封装;DATETIME(3) 验证 |
| 7 | Windows 环境脆弱点 | compose 端口错开;`*.sql` 强制 LF;MinIO round-trip |
| 8 | 密钥管理(双栈同密钥) | `.env` gitignore;`bootstrap_env.py --from-java-env` 只读导入 |
| 9 | 行为暗坑(自增 10000/软删/乐观锁/错误码次序/分页差异) | 黄金主对拍兜底 |
| 10 | **工作量(1012 文件全量)导致中途失控/烂尾** | 全量端点清单做进度分母(已完成/总数随时可量化);域四件套模板化降低边际成本;每域完成即跑该域 parity(持续验收,不设阶段门) |

## 11. 终验收(1:1 完成标准)

1. **端点全覆盖**:extract_endpoints.py 提取的全量 Controller 端点,双栈规范化比对通过(parity ≥ 100% 清单覆盖,行为等价)。
2. **执行器闭环**:mock 执行器全环 e2e + kill-restart 恢复;(有环境时)真实 npm 执行器跑通 runtime_dispatch 场景。
3. **前端全页面与功能闭环**:Playwright 对齐 `frontend/src/features/*` 与路由全集逐页走查,零 404(含 insights/dashboard/environmentVariables/evolution/scheduledTask/integration/settings);且全流程可用:注册→建工作空间→实例化 7 角色小队→建工单→指派→SDLC 推进至 END/交真人→时间线可见。
4. **隔离正确性**:tests/tenant/ 读写路径专项全绿(A/B 工作区互访断言 + 写路径 tenant_id 落库断言)。
5. **定时任务**:18 个任务行为与集群锁语义对拍。
6. **机制专项**:MCP 握手+工具调用;outbox 投递;MinIO+OSS presign round-trip;CliExecutor 四场景(澄清/SDLC 生成/仓库扫描/记忆导入);工作空间软删/恢复(11003/11007)——逐项双栈对拍通过。
7. **工程**:pytest 全绿;ruff/mypy/import-linter 干净;`python -m verify authchain` 全绿。

## 12. 启动命令(实施第一批,Windows 11 / Git Bash)

```bash
# 1) 装 uv 并初始化(uv 自管 CPython 3.12;uv 已于 2026-09-23 安装)
cd <repo>/ai-sdlc && mkdir auto-wonder-py && cd auto-wonder-py
uv init --python 3.12 backend && cd backend && uv python install 3.12

# 2) 同步契约资产(含 frontend/ 源码+dist、TenantTables 清单)+ 生成本地 .env
uv run python scripts/sync_from_java.py && uv run python scripts/bootstrap_env.py

# 3) 起栈 + 冒烟
docker compose -f verify/compose/deps.yml -f verify/compose/app.yml up -d --build
uv run python -m verify smoke
```

## 13. 明确不做

- 不改 `ai-sdlc/auto-wonder/` 任何文件(含 e2e-tests/,社区所有;它是 Java 发布门禁,py 自建 harness)
- 不重设计 schema、不改 Java 侧行为
- Java 481 个测试文件以**等价 pytest 场景**覆盖,不做逐文件字面翻译
- 除此以外**无任何域被排除**:integration(Aone 禁用态代码路径仍完整实现)、evolution、conversation、scheduledtask、im、mcp、backup、insights、environment、debuglog、dashboard、category、guidance、setting、tenant(等价机制)全部在交付内

## 附:关键参考文件(只读)

- `ai-sdlc/auto-wonder/docs/autowonder-schema.sql` — 数据库契约(84 表)
- `ai-sdlc/auto-wonder/docs/scheduler-executor-protocol.md` — 执行器 WS 协议与任务包格式
- `ai-sdlc/auto-wonder/docs/openapi-reference.md` — API 文档子集(交叉校验用;真基准是 Controller 全量提取)
- `ai-sdlc/auto-wonder/docs/autowonder-community-templates.sql` — 小队模板种子
- `ai-sdlc/auto-wonder/src/main/java/com/aliyun/autowonder/tenant/{TenantInterceptor,TenantSqlRewriter,TenantTables}.java` — tenant 隔离契约(38 表清单,以文件实数为准)
- `ai-sdlc/auto-wonder/src/main/java/com/aliyun/autowonder/auth/filter/AuthFilter.java` — 鉴权白名单移植源
- `ai-sdlc/auto-wonder/src/main/java/com/aliyun/autowonder/common/result/Result.java` — 响应信封契约
- `ai-sdlc/auto-wonder/e2e-tests/authchain.sh` — authchain 移植源
- `ai-sdlc/AUTOWONDER_COMMUNITY_SYNC.md` — 同步机制(硬约束 1 的出处)
