# 展馆多语展签审校系统

面向博物馆场景的多语种展签翻译—审校—发布 Web 应用。馆员维护中文展签原文与目标语言；
译者登录后逐条提交译文；**另一名**校对者确认译文；全部译文对应当前原文且已确认后，
馆员发布**不可变语言快照**；游客页始终只展示该语言最新已发布快照。

技术栈：Python 3.12 / FastAPI / SQLite（WAL）/ 原生 HTML + CSS + JavaScript，
无前端构建、无额外运行时。

## 一键启动（Docker Compose）

```bash
docker compose up --build -d
```

容器启动时会自动：

1. 安装 `requirements.txt` 中的依赖；
2. 初始化 SQLite 数据库（位于持久化卷 `museum-data:/data`）；
3. 创建本地账号并（可选）写入演示数据。

访问地址与端口：

| 用途 | 地址 |
| --- | --- |
| 游客参观页（无需登录） | http://localhost:8000/visitor |
| 译者 / 校对者工作台 | http://localhost:8000/workspace （自动跳转登录） |
| 馆员后台（原文、语言、发布） | http://localhost:8000/admin |
| 登录页 | http://localhost:8000/login |
| 健康检查 | http://localhost:8000/api/health |
| OpenAPI 文档 | http://localhost:8000/docs |

停止 / 清理：

```bash
docker compose down          # 停止（数据保留在卷中）
docker compose down -v       # 停止并删除数据库卷
```

## 本地账号（首启自动创建）

| 角色 | 用户名 | 密码 | 权限 |
| --- | --- | --- | --- |
| 馆员 | `curator` | `curator123` | 维护中文原文与目标语言、发布语言快照 |
| 译者 | `translator` | `trans123` | 提交 / 修改逐条译文 |
| 校对者 | `reviewer` | `review123` | 确认译文（不能是该译文提交者本人） |

账号使用 PBKDF2-SHA256 加盐哈希存储在 SQLite 中；会话使用签名 Cookie
（`SessionMiddleware`）。

## 配置（环境变量）

在 `docker-compose.yml` 的 `environment` 中修改：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `SESSION_SECRET` | `dev-insecure-secret-change-me`（代码内置） | 会话 Cookie 签名密钥，**生产必须修改为随机长字符串** |
| `DB_PATH` | `/data/museum.db`（镜像内） | SQLite 数据库文件路径 |
| `SEED_DEMO` | `1` | 首启且库为空时是否写入 4 条示例展签、英语/日语及若干不同状态的译文；设为 `0` 只建账号 |

端口修改：编辑 `docker-compose.yml` 的 `"8000:8000"` 左侧端口，例如
`"9000:8000"` 后访问 http://localhost:9000 。

## 不用 Docker 的本地运行

需要 Python 3.11+：

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
DB_PATH=./museum.db SEED_DEMO=1 uvicorn app.main:app --reload --port 8000
```

数据库在首次请求前自动初始化。

## 业务规则与实现说明

### 修订号与确认失效

- **原文修订号**：每条展签有递增修订号 `current_rev`。馆员每次保存原文都新增一版，
  **即使字句改回原样**也一样；保存后该展签所有语言的译文确认全部清除（待重新审校）。
- **译文修订号**：每条“展签 × 语言”有独立递增修订号。译者每次提交/修改都新增一版，
  旧确认随即清除（确认人、确认时间置空）。
- 译文保存其**所依据的原文修订号** `source_rev`，与当前原文修订号不一致即判为「过期」。

### 工作台状态（按语言分组展示）

| 状态 | 含义 | 是否阻止发布 |
| --- | --- | --- |
| 待译 missing | 尚无译文 | 是 |
| 待审 pending | 译文基于当前原文，但尚未确认 | 是 |
| 过期 stale | 原文已更新，译文基于旧版原文（无论是否曾确认） | 是 |
| 已确认 confirmed | 译文基于当前原文且已经他人确认 | 否 |

发布面板列出每一条不可发布的具体原因（编号 + 状态 + 说明）。

### 发布不可变快照

- 只有该语言**全部展签**译文均为「已确认」时，馆员才能发布；空展签库也不允许发布。
- 每次发布生成递增版本号的快照，把当时的编号、原文文本/修订号、译文文本/修订号
  逐行复制到 `snapshots` / `snapshot_items`，此后任何原文或译文修改都**不会影响**
  历史快照（快照无任何更新/删除接口）。
- 游客页只读取每个语言版本号最大的快照；从未发布的语言显示「暂无内容」。

### 乐观并发控制

提交译文与确认都必须携带**读取时**的修订号：

- 提交译文：`base_rev`（读到的译文修订号，无译文传 `0`）+
  `base_source_rev`（读到的原文修订号）；
- 确认：`base_rev`（读到的译文修订号）；
- 修改原文：`base_rev`（读到的原文修订号）。

与服务器现状不一致（他人已先提交/修改、原文已更新、已确认等）时返回 `409 Conflict`
并附中文原因，**拒绝写入、保持现状**；前端收到 409 自动刷新最新状态。
写事务使用 SQLite `BEGIN IMMEDIATE` 串行化，配合 WAL 模式避免并发覆盖。

### 审校隔离

- 只有「校对者」角色可确认，只有「译者」角色可提交，「馆员」不参与译校。
- 服务端额外校验确认人不得等于该译文修订的提交人，自审返回
  `403 self_review`（同一账号不能审自己的译文）。

## 目录结构

```
app/
  main.py        # FastAPI 路由、工作流与乐观锁
  db.py          # SQLite schema、连接/事务、首启播种
  security.py    # PBKDF2 口令哈希、会话与角色依赖
  static/        # 原生前端页面与 JS/CSS（登录/工作台/后台/游客页）
Dockerfile
docker-compose.yml
requirements.txt
```
