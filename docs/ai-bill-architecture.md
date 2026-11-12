# AI 账单架构与部署

## 入口选择

现有 Vue 页面承载对话、Markdown 和 SSE，Android 原生承载入口、账号验证和 WebView 导航。首版采用 WebView，可以在家中独立发布聊天页。若将来要直接分析手机上尚未同步的账单，可复用同一查询和流式契约来增加本地数据适配，届时按离线交互需求评估原生界面。

## 请求路径

```text
Android ──账号验证──▶ 账本 FastAPI
   │                   │  一次性票据
   ▼                   │
WebView ──HTTPS──▶ book_agent ──受限会话──▶ 账本 FastAPI / MongoDB
                        │
                        └──兼容模型 API──▶ 家中推理服务
```

1. Android 从当前 App 账号发起密码验证，向 AI 账单域名调用 `POST /api/session/ticket`，Agent 将请求交给账本服务的 `POST /auth/ai-bill/ticket`。旧 App 登录资料只有账号标识，进入 AI 账单时需要确认密码。账本后端签发一次性短期票据。
2. WebView 以表单 POST 调用 Agent 的 `/api/session/exchange`。Agent 向记账后端交换只读会话，向 WebView 设置 HTTPS、HttpOnly、SameSite Cookie，然后跳转到聊天页。
3. Vue 通过 `GET /api/session` 获取可用账本模式，通过 `POST /api/chat/stream` 提交问题、模式和对话 ID。账号和家庭范围由账本服务根据会话计算。服务端按已验证的账号、账本模式和家庭范围恢复最近对话。
4. Agent 把规划参数交给 `POST /ai-bill/orders`，家庭请求携带由账本服务返回的家庭范围，后端在查询前重新核对。代码在服务端计算金额并把汇总交给模型。模型和前端使用查询结果生成可核对的回复。账本接口失败时提示查询未完成。

## 配置与发布

家庭部署使用 `18001` 作为 Agent 回环端口、`18002` 作为账本服务回环端口。Cloudflare Tunnel 只将 AI 账单域名转发到 Agent，账本服务独立加载 `MONGO_URI`。

开发分支为 `book_agent/codex/ai-bill-foundation`、`AutoBookKeepingBetaBackend/codex/ai-bill-ledger` 和 `AutoBookKeepingBeta/codex/ai-bill-webview`。

- 使用账本后端的 `app.ai_bill_app:app` 启动专用服务，再启动 Agent 并构建 `frontend/dist`。聊天记忆使用 `AGENT_MONGODB_URI` 和 `AGENT_MONGODB_DB`。部署前确认 MongoDB 数据库和索引权限。
- 家中网关首选 `LLM_PROVIDER=openai_compatible`、`LLM_BASE_URL=https://api.fushengduobuyu.com/v1`、`LLM_MODEL=qwen3.5:9b`。密钥由服务环境注入。真实账本联调使用关闭外部追踪的配置。
- 将选定的 AI 账单子域转发到 Agent 的应用端口。HTTPS 反代为 `/api/chat/stream` 保留 SSE 增量，按家庭网络和模型耗时设置连接和读超时。
- Android 构建时设置 `AI_BILL_ORIGIN`，值为实际的 HTTPS 源。构建配置按 Gradle `-P`、项目 `local.properties`、环境变量依次读取。
- 在隔离环境验证票据过期、票据重复兑换、跨账号请求、家庭权限变化和错误反馈，再用同一月份对照 Android 月报。正式接入真实账本前，确认旧账号密码修复、旧账单查询和家庭写接口的访问控制与迁移方案。

[家庭部署信息清单](home-agent-checklist.md)


## 家庭主机启动与域名映射

在发布目录运行以下命令，启动回环上的 Agent 和账本服务。两端使用各自的 Python 虚拟环境，MongoDB 配置从现有 Agent 环境读取。

```sh
agent/.venv/bin/python agent/deploy/run-home.py --existing-env /root/apps/book-agent/.env
```

在 Cloudflare 的既有 Tunnel 下添加 Published application。主机名使用 `bill.fushengduobuyu.com`，Service 选择 HTTP，地址填 `127.0.0.1:18001`，Path 留空。域名生效后访问 `https://bill.fushengduobuyu.com/ready`，预期返回所选模型和 `status: ok`。
