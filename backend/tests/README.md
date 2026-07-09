# 后端测试

后端测试套件是项目重构的**功能完整性基准（诚实镜像）**：每条测试断言**正确行为**——当前实现对的就绿，当前有 bug 的就红，所见即所实。

## 运行

```bash
cd backend

# 诚实全貌：绿的=对的，红的=现存 bug（红条数 = 待修 backlog）
python -m pytest -q

# 安全网（CI 门禁）：只跑"本该绿"的，红=你引入了新回归=必须修
python -m pytest -m "not known_issue" -q

# bug 看板：只跑已知 bug 测试，红=还没修，绿=已修复待毕业（删 marker）
python -m pytest -m known_issue -q

# 覆盖率
python -m pytest --cov=app --cov-report=term-missing

# 质量门（编译 + ruff + 安全网测试，跨平台，EXIT:0=可部署）
python scripts/run_quality_gate.py
```

> pytest 兼容既有的 `unittest.TestCase` 风格测试，旧测试无需改写。`unittest discover`
> 也可用，但推荐 pytest（支持 fixture / marker / 参数化）。

## 目录结构

```
backend/
├── conftest.py            # 全局：日志静默、known_issue marker 注册、db_factory fixture
├── pytest.ini             # testpaths / markers / strict-markers
├── tests/
│   ├── support/           # 共享脚手架（消除过去 29× 复制的 _make_test_app 样板）
│   │   ├── db.py          #   make_sqlite_engine / make_session_factory / create_tables
│   │   ├── app.py         #   make_test_app（复用生产中间件/handler）/ make_client
│   │   ├── seed.py        #   seed_user / auth_cookies / login_session_cookie
│   │   └── llm.py         #   patch_call_llm / make_recorded_result
│   ├── fixtures/real_llm_failures/   # 真实 LLM 失败样本回归（亮点，持续扩充）
│   └── test_*.py          # 各被测域测试
└── requirements-dev.txt   # pytest / pytest-cov
```

## 写新测试

**优先用 `tests/support/`**，不要复制脚手架：

```python
from tests.support import make_sqlite_engine, make_session_factory, create_tables, make_test_app, seed_user, auth_cookies

def test_xxx():
    engine = make_sqlite_engine()
    factory = make_session_factory(engine)
    create_tables(engine, [User, Project, Chapter])   # 选择性建表，避开 pgvector DDL
    seed_user(factory, user_id="u1")
    app = make_test_app(factory, [chapters_router])    # 复用真实 auth + 错误 handler
    client = make_client(app)
    auth_cookies(client, "u1")                         # 跳过登录注入会话
    ...
```

## known_issue 工作流（诚实镜像）

测试套件对已知 bug 采用**诚实镜像**语义——绝不靠 xfail 把失败藏成绿色：

- **当前正确行为** → 普通通过测试（属于安全网，必须保持绿）。
- **已知 bug**（实现有缺陷）→ `@pytest.mark.known_issue`，断言**正确**行为；当前实现有 bug
  故测试**真跑真红**，默认 `pytest -q` 即可见。三条视图：
  - `pytest -q`：诚实全貌（安全网绿 + 已知 bug 红）。
  - `pytest -m "not known_issue"`：安全网（CI 门禁，必须全绿 = 可部署）。
  - `pytest -m known_issue`：bug 看板（红=待修，绿=已修复）。
- **bug 修复后**：该测试从红变绿 → 手动删除 `@pytest.mark.known_issue` marker，
  让它从"看板"毕业进入"安全网"（此后若再红即为真回归）。定期跑 `-m known_issue`
  巡检：凡已通过的都该毕业，防止 marker 腐化。

每个 `known_issue` 注释都标注对应的 `项目情况完全分析.md` 问题编号（如 `# H15`）。

## 测试纪律（§6.6）

1. 新增 service/route 必须带直接测试；删功能必删测试（§6.2 四处同步）。
2. 断言结构化字段/键名，**用户可见文案不进断言**（中文文案微调即脆断）。
3. 测试**禁止深挖路由私有函数**——若不得不测私有 helper，说明它该下沉为 service。
4. coverage 基线只升不降。
