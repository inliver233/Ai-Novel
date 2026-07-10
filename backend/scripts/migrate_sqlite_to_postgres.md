# SQLite → Postgres 迁移（Phase 3.4 / LMEM-180）

目标：把现有 SQLite（`ainovel.db`）搬运到 Postgres（保留所有 id），用于后续 `pgvector` 与生产化部署。

## 前置条件

- Postgres 15+（目标库建议为空库）
- 账号具备执行扩展的权限（或 DBA 预先安装）：
  - `uuid-ossp`
  - `pg_trgm`
- 后端 Python 依赖已安装（需要 Postgres driver，例如 `psycopg2-binary`）

## 迁移步骤（推荐）

1) **备份 SQLite 源库**（只读搬运；回滚依赖这个备份）

- 复制 `backend/ainovel.db` 到安全位置（不要在原文件上直接试验）。

2) **准备 Postgres 空库**

- 创建数据库与账号，并授予权限（略）。

3) **在目标库执行 Alembic 迁移（建表 + 扩展）**

> 你也可以跳过本步，让脚本自动跑（默认会 `alembic upgrade head`）。

```powershell
cd backend
$env:APP_ENV = "dev"
$env:TASK_QUEUE_BACKEND = "inline"
$env:DATABASE_URL = "postgresql://user:pass@host:5432/ainovel"
.\.venv\Scripts\python.exe -c "from app.db.migrations import ensure_db_schema; ensure_db_schema(); print('schema ok')"
```

4) **搬运数据（逐表，保留 id）**

脚本会在复制前反射源/目标 schema，并按外键依赖自动生成完整表顺序：

- 源路径必须是已存在且包含 `users` / `projects` 的可识别 Ai-Novel SQLite 库，路径拼错或空库会立即失败；
- 复制所有同时存在于 SQLite 源库与当前 PostgreSQL schema 的业务表；
- 跳过可由 `search_documents` 重建的 SQLite FTS5 虚表/影子表；
- 对旧库中的 14 张退役表单独执行数据保留门禁：任一表非空即在复制前失败，并要求先运行
  `python scripts/archive_retired_tables.py --database-url <source-url> archive --output <archive-dir>`，再运行
  `python scripts/archive_retired_tables.py --database-url <source-url> purge --archive <archive-dir> --confirm PURGE_RETIRED_TABLE_DATA`
  完成校验和清空；只有已验证为空的退役表才会被显式跳过；
- 退役表空检查、业务表复制和最终 count/hash 验证共用同一个 `BEGIN IMMEDIATE` SQLite 连接；这段时间
  源库写入会被阻塞，避免空表检查后又写入退役数据而被静默漏迁；
- PostgreSQL 专属 `vector_chunks` 不从 SQLite 复制，向量数据需按现有索引流程重建；
- 若源库存在目标 schema 无法承接的业务表，会在写入任何业务数据前中止并列出表名；
- 保留自增主键后会重置 PostgreSQL sequence，避免迁移后的新写入与旧 id 冲突。

```powershell
cd backend
.\.venv\Scripts\python.exe scripts\migrate_sqlite_to_postgres.py `
  --source .\\ainovel.db `
  --target postgresql://user:pass@host:5432/ainovel `
  --report .\\sqlite_to_postgres.report.json
```

可选参数（建议先跑一遍 dry-run 熟悉流程）：

- `--dry-run`：只输出计划；默认在本地临时 SQLite 中模拟当前 Alembic head，不写入目标库 schema 或业务数据
- `--resume`：幂等断点续跑（Postgres：`ON CONFLICT DO NOTHING`；要求表有主键）
- `--no-migrate-schema`：跳过目标库的 `alembic upgrade head`（已手工跑过迁移时使用）
- `--chunk-size`：单表批量写入大小（默认通常够用；大库可调）

> 注意：脚本会在控制台输出 `[target]`，但会对 URL 中的密码做掩码；report 以计数/抽样 hash 为主，不包含明文 API Key（建议不要提交 report 文件）。

如果中途失败/中断，直接重跑并开启幂等模式（断点续跑）：

```powershell
.\.venv\Scripts\python.exe scripts\migrate_sqlite_to_postgres.py `
  --source .\\ainovel.db `
  --target postgresql://user:pass@host:5432/ainovel `
  --resume `
  --report .\\sqlite_to_postgres.report.json
```

## 验证清单

脚本会写入 report（JSON）：

- 实际 `table_order`、`skipped_metadata_tables`，以及单独列出的
  `skipped_empty_retired_tables`（后者必须仅包含已检查为空的旧退役表）；
- 每表：`source_count` / `target_count`
- 抽样：`sample_hash_source` / `sample_hash_target`
- 外键抽检：`missing_fk_total`（应为 `0`）
- Postgres 扩展检测：`postgres_extensions.uuid-ossp/pg_trgm`（应为 `true`）

## 启动服务验证（目标库）

```powershell
cd backend
$env:APP_ENV = "dev"
$env:TASK_QUEUE_BACKEND = "inline"
$env:DATABASE_URL = "postgresql://user:pass@host:5432/ainovel"
.\.venv\Scripts\python.exe -m uvicorn app.main:app --workers 1 --port 8000
```

## 回滚策略

- **SQLite 源库永远保留**（不要在原文件上试验；生产数据务必备份）
- Postgres 侧若出现问题：**直接 drop/recreate** 目标库，再按上面流程重来
