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
                        └──模型适配器──▶ 家中 Ollama 或 OpenAI Responses
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
- 官方 OpenAI API 使用 `LLM_PROVIDER=openai`、`https://api.openai.com/v1` 和本轮选定的 `gpt-6.1-terra`。密钥仅从私有服务器环境读取，不能注入页面或 APK；`store=false` 关闭供应商响应存储，必要账本证据仍会发送给所选模型。应用历史和各阶段用量在内部持久化。切换 API 无需重启或改动已有 Ollama 服务。
- 将选定的 AI 账单子域转发到 Agent 的应用端口。HTTPS 反代为 `/api/chat/stream` 保留 SSE 增量，按家庭网络和模型耗时设置连接和读超时。
- Android 构建时设置 `AI_BILL_ORIGIN`，值为实际的 HTTPS 源。构建配置按 Gradle `-P`、项目 `local.properties`、环境变量依次读取。
- 在隔离环境验证票据过期、票据重复兑换、跨账号请求、家庭权限变化和错误反馈，再用同一月份对照 Android 月报。正式接入真实账本前，确认旧账号密码修复、旧账单查询和家庭写接口的访问控制与迁移方案。

[家庭部署信息清单](home-agent-checklist.md)


## 家庭主机启动与域名映射

正式环境沿用家庭主机的 apps/runtime/secrets/infra 划分。Agent源码在 `/root/apps/book-agent`，账本专用服务在 `/root/apps/ai-bill/ledger`；环境分别在 `/root/runtime/envs/book-agent`、`/root/runtime/envs/ai-bill-ledger`。状态和私有评测在 `/root/runtime/state/book-agent`，缓存及历史源码归档在 `/root/runtime/cache/book-agent`，环境配置在 `/root/secrets/book-agent/{agent,ledger}.env`。凭据不进入仓库。

`app-ai-bill-agent.service` 和 `app-ai-bill-ledger.service` 均enabled/active，通过原有 `apps.target` 开机启动。单元源文件、环境锁、清单、迁移日志及部署工具统一由 `/root/infra` 管理，安装单元使用其 `scripts/deploy-unit.sh`。变更前保存私有配置备份，候选先在模块缓存验证，按依赖顺序切换并验证就绪、资源、权限和真实会话，失败恢复原代码与配置。不得在root顶层或另起 `/opt/ai-bill` 放临时部署树。

服务统一通过 systemd 管理。旧的前台双进程启动脚本已移除，避免使用过时的兼容接口配置或漏掉会话与成员键环境。排障时检查现有单元及日志：

```sh
systemctl status app-ai-bill-agent.service app-ai-bill-ledger.service app-ollama.service --no-pager
journalctl -u app-ai-bill-agent.service -n 80 --no-pager
```

在 Cloudflare 的既有 Tunnel 下添加 Published application。主机名使用 `bill.fushengduobuyu.com`，Service 选择 HTTP，地址填 `127.0.0.1:18001`，Path 留空。域名生效后访问 `https://bill.fushengduobuyu.com/ready`，预期返回所选模型和 `status: ok`。

## 分析和历史接口

规划器选择授权查询模板与可组合计算工具。日期窗口在程序中展开，各任务使用 queryIds 绑定查询来源。金额按 Android 的正负号识别收支。计算工具返回结构、趋势、变化贡献、异常线索、周期、条件预测、情景、预算及储蓄证据，模型负责解释。空白月份、未完月份和缺失用户输入都保留明确标记。

历史 API 为 `/api/conversations` 与 `/api/conversations/{id}`，支持游标分页。读取前重新验证账号及当前家庭范围。`newSession=true` 在首条消息时创建新会话；刷新默认恢复最近会话。MongoDB 消息元数据保存执行计划、证据、模型标识、分析版本与耗时，便于对错误定位和回归。

发布前运行单元测试、前端构建及 `python -m scripts.evaluate_analysis`。模型评测使用虚构账本和内存会话，同时检查工具选择和关键数值，再人工检查解释中的条件、范围与因果表述。通过 `/ready` 仅证明记忆配置存在且模型在网关列表中；数据库连通性和授权聊天需另行验证。

复杂分析先解析用户条件、核对计划、执行计算和必要的来源调查，再由模型选择已绑定的事实提纲，最后由程序生成计算发现与解释。原始计算、已解析条件、报告引用、修改记录和校对结果保存在回答元数据中。个人/家庭、不同筛选范围以及并列情景分别保留条件，不能互相借用余额；追问同一来源时替换旧条件，同轮多个独立增量则全部保留。

当前分析链路把主任务和备注核查等子任务分开保存。继续追问原结论时恢复目标月份、历史样本、来源归组与用户条件；切换到另一段历史来源查询时退役旧任务，避免后续又恢复无关预测。调查员读取初次计算结果和实际备注，可在同一授权快照内补充有界聚合分析；它不能扩大已经查询的日期覆盖或虚构余额。

`aggregate` 使用声明式分组、筛选与最多八个比较区间，不执行模型生成的 SQL 或代码。它返回实际查询覆盖、筛选审计、完整范围的去重总计和有上限的分组行。确定性表格保留全部已返回分组，即使叙述只选择主要发现。财务叙述从已绑定的计算发现生成；备注里的编号不能被当成金额。提纲错误或越权时由程序回退，不再使用自由财务写作或模型自审。

共享 GPU 的发布验收还需记录显存峰值、其他任务占用、上下文长度、GPU 层数和空闲释放时间。降低驻留时间只减少空闲占用，不能替代运行峰值测试。参数通过 Agent 请求传给 Ollama，无需更改其他 GPU 服务。

## 结构化推理运行时

家庭主机原有 Ollama 0.21.2 对 Qwen3.5 的 `think=false` 加 JSON Schema 请求曾直接返回普通中文，导致规划或报告在读取账本前后反复失败。这与 [Ollama 上游问题及修复](https://github.com/ollama/ollama/pull/15901) 对应；发布前必须验证实际协议能力，不能只检查版本或模型名称。

固定运行时在 `/root/runtime/bin/ollama-v0.40.16`，官方发布包的大小和 SHA-256 直接与 GitHub 发布元数据核对后使用。此前以临时 `app-ai-bill-ollama.service` 绑定 `127.0.0.1:11435` 验证，复用现有模型目录；原服务在验证期间保持原配置。该临时单元现已停用并归档；未来比较应通过infra流程部署引用canonical运行时的新验证单元。

仅选择本地Qwen作为生产模型并明确切换其运行时时，顺序是启动验证实例、通过 `python -m scripts.check_model_contract`、完成模型质量回放，再停止验证实例，通过现有 `app-ollama.service` 的 drop-in 切换到已验证的二进制。原来的监听地址、模型目录和账号由现有服务继续配置，Agent 继续使用 `127.0.0.1:11434`，其他现有 API 路由无需改变。仓库提供 `deploy/ollama-runtime.conf.example`。生产复用同一个运行时，保持单路推理、最多一个驻留模型和一分钟空闲释放；模型不随应用发布复制。比较两个运行时时串行测试，等待前一个模型释放。Agent 发布和运行时配置都保留原版本，方便回滚。

当前默认选择官方 OpenAI API 的 Terra，依据与残留失败见[模型对照](model-comparison-2026-11-19.md)。本地Qwen只在独立验证端口比较；正式Agent切到API时，仅发布Agent，不升级或重启现有Ollama服务。验证实例在回放完成后停止。


## 正式账号与API就绪探测

正式Agent和账本单元遵循主机原设计，以root运行，`DynamicUser=no`，不新增服务账号。保留 `NoNewPrivileges`、只读系统及主目录、私有临时目录、空能力集，只为模块状态和缓存配置写权限；源码和环境只读，凭据目录、SSH/Codex目录及infra备份对进程不可见。环境文件以root0600保存在secrets，由systemd在隔离前读取。发布预检必须复用实际单元的namespace保护，确认代码/依赖可读、状态可写且凭据不可读。仅在root交互shell导入成功不能证明隔离单元可用。

Python环境在最终绝对路径新建并核对固定依赖，不能直接移动含绝对shebang的venv。删除旧树前先完成真实授权会话验收、确认进程/单元/配置无旧路径依赖、保留源码与必要回滚配置，并检查目标与所有子目录均无独立挂载。完整当前布局及验证见[发布与归位记录](deployments/2026-11-20.md)。

官方API的/ready探测使用20秒HTTP客户端超时，成功模型列表探测在本进程缓存最多30秒并合并并发请求；配置或API密钥变化立即重新探测。失败不缓存，新探测发现模型消失或连接失败仍返回503，未配置记忆也返回503。HTTP响应携带no-store。外部发布探测超时应大于该探测预算（本次使用30秒），并在同正式账号及保护配置的隔离端口通过后切换。/ready不验证回答语义，真实授权问题和历史恢复仍须单独验收。
