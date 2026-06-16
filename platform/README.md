# OpenClaw Platform

OpenClaw Platform 是一个基于 FastAPI 的多租户网关服务，用于管理和运行 OpenClaw 实例。

## 功能特性

- **用户管理** - 注册、登录、JWT 认证、角色权限（admin/user）
- **配额管理** - 免费/基础/专业三级配额，限制每日 token 使用量
- **容器管理** - 为每个用户创建和管理独立的 Docker 容器
- **LLM 代理** - 统一路由 LLM 请求，平台端存储 API Keys，容器内不暴露密钥
- **使用统计** - 记录并统计每个用户的 LLM token 使用量

## 技术栈

- **Web 框架**: FastAPI + Uvicorn
- **数据库**: PostgreSQL + SQLAlchemy (async) + Alembic
- **认证**: JWT (python-jose) + bcrypt
- **容器**: Docker SDK for Python

## 架构概览

```
┌─────────────┐     ┌─────────────────┐     ┌──────────────────┐
│   Client    │────▶│  Platform API   │────▶│  User Container   │
│  (Frontend) │     │   (port 8080)    │     │  (openclaw web)   │
└─────────────┘     └────────┬────────┘     └──────────────────┘
                             │
                             ▼
                    ┌────────────────┐
                    │   PostgreSQL    │
                    └────────────────┘
```

## API 端点

| 路由 | 描述 |
|------|------|
| `GET /api/ping` | 健康检查 |
| `POST /api/auth/register` | 用户注册 |
| `POST /api/auth/login` | 用户登录 |
| `POST /api/auth/refresh` | 刷新 Token |
| `POST /api/auth/container` | 获取用户容器访问信息 |
| `POST /api/llm/v1/*` | LLM 代理接口 |
| `GET /api/admin/users` | 管理员获取用户列表 |
| `PUT /api/admin/users/{user_id}` | 管理员更新用户 |
| `DELETE /api/admin/users/{user_id}/container` | 管理员删除用户容器 |
| `GET /api/admin/usage/summary` | 平台使用统计 |
| `/api/openclaw/*` | 代理到 OpenClaw Gateway（见 platform/app/routes/proxy.py） |

## 配置说明

通过环境变量配置（以 `PLATFORM_` 为前缀）：

| 变量 | 默认值 | 描述 |
|------|--------|------|
| `PLATFORM_DATABASE_URL` | `postgresql+asyncpg://nanobot:nanobot@localhost:5432/nanobot_platform` | 数据库连接 |
| `PLATFORM_JWT_SECRET` | `change-me-in-production` | JWT 密钥 |
| `PLATFORM_DEFAULT_MODEL` | `claude-sonnet-4-5` | 新用户默认模型 |
| `PLATFORM_ROUTE_MODEL` | 空 | 强制所有 LLM 请求路由到指定模型；为空时尊重请求模型 |
| `PLATFORM_OPENCLAW_IMAGE` | `openclaw:latest` | Docker 镜像 |
| `PLATFORM_CONTAINER_MEMORY_LIMIT` | `512m` | 容器内存限制 |
| `PLATFORM_QUOTA_FREE` | `100000` | 免费用户每日配额 |

## 数据模型

- **User** - 用户账户（username, email, password_hash, role, quota_tier）
- **Container** - 用户容器元数据（docker_id, status, internal_host, internal_port）
- **UsageRecord** - LLM 使用记录（model, input_tokens, output_tokens）
- **AuditLog** - 操作审计日志

## 快速开始

```bash
# 安装依赖
cd platform
pip install -e .

# 启动服务（需要 PostgreSQL）
export PLATFORM_DATABASE_URL="postgresql+asyncpg://user:pass@localhost:5432/nanobot_platform"
python -m app.main
```

## Docker 部署

```bash
# 使用 docker-compose（参考项目根目录的 docker-compose.yml）
docker-compose up -d platform
```

## 前端确定用户容器是否启动的原理

请求处理流程

当前端请求 `http://<host>:8080/api/openclaw/sessions/web%3Adefault` 时：

1. **入口：proxy.py**
   请求首先到达 `platform/app/routes/proxy.py` 的 `proxy_http` 函数：

   ```python
   @router.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
   async def proxy_http(path: str, ...):
       base_url = await _container_url(db, user)  # <-- 关键步骤
   ```

2. **容器状态检查：ensure_running**
   `_container_url` 调用 `ensure_running` 函数（`platform/app/container/manager.py`），这个函数会：

   1. 从数据库查询容器记录 - 检查该用户是否有容器记录
   2. 根据状态处理：
      - None → 创建新容器
      - paused → unpause 恢复运行
      - archived → 重新创建
      - running → 验证容器实际运行状态

3. **Docker API 实际检查**
   在 `ensure_running` 中：

   ```python
   elif record.status == "running":
       try:
           c = client.containers.get(record.docker_id)
           if c.status != "running":
               c.start()  # 如果状态不是 running，启动它
       except DockerNotFound:
           # 容器被外部删除，重新创建
           return await create_container(db, user_id)
   ```
