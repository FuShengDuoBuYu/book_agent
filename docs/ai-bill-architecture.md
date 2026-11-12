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

Agent 默认分支为 `main`，账本后端为 `python`，Android 为 `master`。功能变更使用 `codex/` 分支，通过独立 PR 合并。

- 使用账本后端的 `app.ai_bill_app:app` 启动专用服务，再启动 Agent 并构建 `frontend/dist`。聊天记忆使用 `AGENT_MONGODB_URI` 和 `AGENT_MONGODB_DB`。部署前确认 MongoDB 数据库和索引权限。
- 家中 Agent 可通过 Ollama 原生接口调用 `qwen3.5:9b`，配置 `LLM_PROVIDER=ollama`、`LLM_BASE_URL=http://127.0.0.1:11434`。原生接口支持规划 JSON Schema、`OLLAMA_NUM_CTX` 和回答阶段 `LLM_REASONING`。兼容网关仍可配置 `openai_compatible` 与 `/v1` 地址，密钥由服务环境注入。真实账本联调关闭外部追踪。
- 将选定的 AI 账单子域转发到 Agent 的应用端口。HTTPS 反代为 `/api/chat/stream` 保留 SSE 增量，按家庭网络和模型耗时设置连接和读超时。
- Android 构建时设置 `AI_BILL_ORIGIN`，值为实际的 HTTPS 源。构建配置按 Gradle `-P`、项目 `local.properties`、环境变量依次读取。
- 在隔离环境验证票据过期、票据重复兑换、跨账号请求、家庭权限变化和错误反馈，再用同一月份对照 Android 月报。正式接入真实账本前，确认旧账号密码修复、旧账单查询和家庭写接口的访问控制与迁移方案。

[家庭部署信息清单](home-agent-checklist.md)


## 家庭主机启动与域名映射

正式环境由 `app-ai-bill-agent.service` 和 `app-ai-bill-ledger.service` 管理，需保持 enabled 和 active。Agent 环境位于 `/etc/ai-bill/agent.env`，凭据不进入仓库。新版本使用独立发布目录和 systemd drop-in 切换，保留旧发布目录及受限权限的配置备份用于回滚。

以下命令仅用于在发布目录前台启动两项服务进行排障。两端使用各自的 Python 虚拟环境，MongoDB 配置从现有 Agent 环境读取；已经运行 systemd 服务时不要重复占用端口。

```sh
agent/.venv/bin/python agent/deploy/run-home.py --existing-env /root/apps/book-agent/.env
```

在 Cloudflare 的既有 Tunnel 下添加 Published application。主机名使用 `bill.fushengduobuyu.com`，Service 选择 HTTP，地址填 `127.0.0.1:18001`，Path 留空。域名生效后访问 `https://bill.fushengduobuyu.com/ready`，预期返回所选模型和 `status: ok`。

## 分析和历史接口

规划器选择授权查询模板与可组合计算工具。日期窗口在程序中展开，各任务使用 queryIds 绑定查询来源。金额按 Android 的正负号识别收支。计算工具返回结构、趋势、变化贡献、异常线索、周期、条件预测、情景、预算及储蓄证据，模型负责解释。空白月份、未完月份和缺失用户输入都保留明确标记。

历史 API 为 `/api/conversations` 与 `/api/conversations/{id}`，支持游标分页。读取前重新验证账号及当前家庭范围。`newSession=true` 在首条消息时创建新会话；刷新默认恢复最近会话。MongoDB 消息元数据保存执行计划、证据、模型标识、分析版本与耗时，便于对错误定位和回归。

发布前运行单元测试、前端构建及 `python -m scripts.evaluate_analysis`。模型评测使用虚构账本和内存会话，同时检查工具选择和关键数值，再人工检查解释中的条件、范围与因果表述。通过 `/ready` 仅证明记忆配置存在且模型在网关列表中；数据库连通性和授权聊天需另行验证。

复杂分析先解析用户条件，再执行计算、构造带编号的证据句、生成解释并校对，核验完成后才发送正文。原始计算、已解析条件、报告引用、修改记录和校对结果保存在回答元数据中。个人/家庭、不同筛选范围以及并列情景分别保留条件，不能互相借用余额；追问同一来源时替换旧条件，同轮多个独立增量则全部保留。

共享 GPU 的发布验收还需记录显存峰值、其他任务占用、上下文长度、GPU 层数和空闲释放时间。降低驻留时间只减少空闲占用，不能替代运行峰值测试。参数通过 Agent 请求传给 Ollama，无需更改其他 GPU 服务。
