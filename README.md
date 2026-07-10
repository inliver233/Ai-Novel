# Ai-Novel Lite

一个面向中文长篇创作场景的 **AI 小说创作平台**。  
项目提供从立项、大纲、角色、章节写作到提示词调试、模型配置、批量生成与检索增强的一整套工作流，并支持使用 **Docker Compose** 进行快速部署。

---

## 社区与友链

本开源项目已链接认可 **LINUX DO 社区**：

- Linux DO：<https://linux.do>

欢迎关注、交流与反馈。

---

## 项目简介

Ai-Novel Lite 旨在为小说创作者提供一个可自托管、可扩展、可持续迭代的 AI 写作环境。

它不只是一个“调用大模型生成文本”的界面，而是把小说创作过程中常见的核心对象结构化下来，例如：

- 项目
- 角色
- 大纲 / 细纲
- 章节
- 世界观 / 条目 / 记忆
- Prompt 模板
- LLM 配置与任务预设

通过这些结构化信息，系统可以在生成时提供更稳定的上下文、更清晰的创作约束，以及更适合长篇内容持续写作的工作流。

---

## 核心功能

### 1. 小说项目管理

- 创建与管理多个小说项目
- 配置项目基础信息、风格、约束与写作设置
- 支持多项目切换与独立数据隔离

### 2. 大纲与细纲工作流

- 维护主线大纲
- 支持多大纲结构
- 支持细纲生成与章节规划
- 支持大纲解析与结构化处理

### 3. 角色 / 设定 / 条目管理

- 角色信息管理
- 世界观与设定条目管理
- 便于在创作时持续引用和维护上下文一致性

### 4. 章节写作与 AI 生成

- 章节列表与编辑
- AI 辅助生成章节内容
- 批量生成任务
- 生成记录与运行状态查看

### 5. Prompt Studio

- Prompt 模板管理
- Prompt 预设与任务预设
- 可视化调试不同生成任务的输入输出

### 6. 多模型 / 多配置支持

- 支持多种 LLM 配置档案
- 支持模型能力查询、任务预设、参数预设
- 适合不同任务使用不同模型或不同参数组合

### 7. 检索增强与记忆机制

- 向量检索
- 故事记忆 / 结构化记忆
- 项目知识与上下文增强

### 8. 用户与权限

- 本地注册 / 登录
- 管理员能力
- 多用户基础支持

### 9. 导入导出

- 支持项目导入 / 导出
- 便于迁移、备份与交付

---

## 技术栈

### 前端

- React
- TypeScript
- Vite
- Tailwind CSS
- Nginx

### 后端

- FastAPI
- SQLAlchemy
- Alembic
- Redis + RQ
- Pydantic

### 存储与运行

- PostgreSQL
- Docker Compose

---

## 项目结构

```text
.
├─ frontend/                # React 前端
├─ backend/                 # FastAPI 后端
├─ docker-compose.yml       # 生产部署主文件
├─ .env.example             # 环境变量模板
└─ README.md
```

---

## 快速开始

### 方式一：Docker Compose 部署（推荐）

#### 1. 克隆项目

```bash
git clone -b lite https://github.com/inliver233/Ai-Novel.git
cd Ai-Novel
```

#### 2. 准备环境变量

```bash
cp .env.example .env
```

建议至少修改以下内容：

- `POSTGRES_PASSWORD`
- `AUTH_ADMIN_USER_ID`
- `AUTH_ADMIN_PASSWORD`

#### 3. 启动服务

```bash
docker compose up -d --build
```

#### 4. 访问项目

- 前端：`http://<服务器IP>:5173`

默认部署策略：

- 前端对外开放 `5173`
- 后端仅监听宿主机本地 `127.0.0.1:8000`
- PostgreSQL 镜像内置 pgvector；启动迁移会安装 `vector` 扩展并确保 `vector_chunks` 可用
- PostgreSQL / Redis 不直接暴露公网端口

如通过 `DATABASE_URL` 接入外部 PostgreSQL，请先确保服务端已安装 pgvector，且数据库账号可执行
`CREATE EXTENSION vector`；迁移会在能力缺失时明确失败，不会静默降级为不可持久化的进程内索引。

#### 5. 更新项目

```bash
git pull
docker compose up -d --build
```

---

## 本地开发

项目保留了本地开发方式：

```bash
python start.py
```

该脚本会同时启动：

- 前端开发服务器
- 后端开发服务器

但在正式部署场景中，建议始终使用 Docker Compose。

---

## 测试

测试套件是功能完整性的**诚实镜像基准**：每条测试断言**正确行为**——对的绿、有 bug 的红，所见即所实。CI（`.github/workflows/ci.yml`）在 push / PR 时以后端 `-m "not known_issue"`、前端 `--tagsFilter '!@known_issue'` 安全网及其覆盖率基线为合并门禁。

### 后端

```bash
cd backend
python -m pip install -r requirements-dev.txt # 锁定的生产依赖 + 测试/质量工具

python -m pytest -q                       # 诚实全貌（绿的=对的，红的=现存 bug）
python -m pytest -m "not known_issue" -q --cov=app --cov-report=term --cov-fail-under=62 # 安全网/coverage 门禁
python -m pytest -m known_issue -q        # bug 看板（红=待修，绿=已修复待毕业）
python scripts/run_quality_gate.py        # 编译 + ruff + 安全网测试 + coverage≥62%
```

详见 [`backend/tests/README.md`](backend/tests/README.md)。

### 前端

```bash
cd frontend
npm ci

npx vitest run                            # 诚实全貌（安全网绿 + 已知 bug 红）
npx vitest run --coverage --tagsFilter '!@known_issue' # 安全网/coverage 门禁
npx vitest run --tagsFilter '@known_issue'  # bug 看板（红=待修，绿=待毕业）
npx tsc -p tsconfig.test.json --noEmit    # 测试类型检查
npm run build                             # 生产构建门禁
```

测试统一收纳在 `frontend/tests/`，支持 jsdom + @testing-library 的组件交互测试。详见 [`frontend/tests/README.md`](frontend/tests/README.md)。

---

## 数据持久化

Docker Compose 默认会创建以下卷：

- `ainovel_postgres_data`
- `ainovel_app_data`

其中：

- PostgreSQL 主数据与 pgvector 索引保存在 `ainovel_postgres_data`
- 应用运行数据、可选 Chroma 目录与自动生成密钥等保存在 `ainovel_app_data`

如需完全清空部署数据：

```bash
docker compose down -v
```

---

## 现有 SQLite 数据迁移

如果你之前使用的是本地 SQLite 数据库（如 `backend/ainovel.db`），它不会自动进入 Docker 部署后的 PostgreSQL。

项目已提供迁移工具：

- `backend/scripts/migrate_sqlite_to_postgres.py`
- `backend/scripts/migrate_sqlite_to_postgres.md`

请按文档完成迁移。

### 旧版已退役数据表归档（SQLite）

升级到移除旧版结构化记忆、Worldbook、项目表格等已退役表的版本前，可先执行：

```bash
cd backend
python scripts/archive_retired_tables.py --database-url sqlite:///./ainovel.db preflight
python scripts/archive_retired_tables.py --database-url sqlite:///./ainovel.db archive --output ./retired-table-archive
python scripts/archive_retired_tables.py --database-url sqlite:///./ainovel.db purge \
  --archive ./retired-table-archive --confirm PURGE_RETIRED_TABLE_DATA
```

归档目录包含逐表规范化 JSON、行数与 SHA-256 校验清单。`purge` 会再次核对归档与实时数据库完全一致，
并在单个事务中按外键顺序清空这些表；校验失败或存在并发写入时不会删除数据。若需要恢复，请先降级/重建兼容表结构，再执行：

```bash
python scripts/archive_retired_tables.py --database-url sqlite:///./ainovel.db restore \
  --archive ./retired-table-archive
```

该工具只支持 SQLite。PostgreSQL 部署必须使用 `pg_dump` / `pg_restore` 做完整备份；后续清理迁移遇到非空退役表时会中止，禁止静默删除历史数据。

---

## 适用场景

Ai-Novel Lite 适合：

- 个人作者自建 AI 小说工作台
- 小说项目长期维护
- 需要结构化管理角色 / 大纲 / 章节 / Prompt 的创作团队
- 希望把“模型调用”升级为“完整创作流程”的用户

---

## 说明

本分支为 **lite** 部署整理分支，重点是：

- 使仓库更适合直接克隆部署
- 使 Docker Compose 部署路径更清晰
- 避免把本地数据库、缓存和构建产物提交进仓库

---

## License

如需开源许可说明，可在后续补充 `LICENSE` 文件。
