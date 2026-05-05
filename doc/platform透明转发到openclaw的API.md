 核心的透明转发代码主要在以下几个文件中：

  主要转发入口

  1. platform/app/routes/proxy.py — 这是最主要的反向代理文件

  - 第 37-80 行 _container_api_request() — 通用 HTTP 转发函数，将请求用 httpx.AsyncClient 转发到用户的 openclaw 容器
  - 第 434-504 行 proxy_http() — 核心的 catch-all HTTP 反向代理端点 {"/api/openclaw/{path:path}"}，支持 GET/POST/PUT/DELETE/PATCH，有两个分支：
    - Shared 模式 (446行)：委托给 shared_runtime_request() 转发到共享 openclaw
    - Dedicated 模式 (472行)：通过 _container_url() 获取用户专属容器地址，用 httpx 转发
  - 第 328-363 行 proxy_events_stream() — SSE 事件流代理 (GET /api/openclaw/events/stream)
  - 第 511-596 行 proxy_websocket() — WebSocket 双向代理 (WS /api/openclaw/ws)
  - 第 598-673 行 proxy_terminal_websocket() — 终端 WebSocket 代理 (WS /api/openclaw/terminal/ws)

  2. platform/app/routes/shared_openclaw.py — 共享 openclaw 运行时的代理路由（前缀 /api/shared-openclaw），所有端点都调用
  shared_runtime_request() 转发

  3. platform/app/shared_runtime.py — 共享运行时的 HTTP 客户端，第 40-82 行 shared_runtime_request() 是共享模式的核心转发函数，用 httpx
  将请求发送到 settings.shared_openclaw_url

  转发架构总结

  前端请求 /api/openclaw/*
      → proxy.py (FastAPI catch-all)
          → httpx → 用户专属 Docker 容器 (port 18080)
                       或
          → shared_runtime.py → 共享 openclaw 实例

  proxy.py 第 434 行的 proxy_http 函数是主要的透明转发入口，它根据运行模式决定转发目标（专属容器 / 共享实例 / 本地开发 URL）。
