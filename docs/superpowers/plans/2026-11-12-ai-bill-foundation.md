# AI 账单基础架构实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task.

**目标** — 保留现有 Vue 聊天页，通过已验证的 Android 登录交接使用只读账本和家中模型。

**架构** — Android 向账本后端验证账号并取得一次性票据。WebView 把票据提交给同源 Agent，Agent 交换到仅能查账的会话，分别调用账本 API 和模型 API。账本后端决定查询范围，统计代码负责金额。

**技术栈** — Android Java、FastAPI、MongoDB、Vue、LangChain。

**设计说明** — [架构与部署](../../ai-bill-architecture.md)。

## 约束

- 三个仓库独立提交。以现有代码和接口为基础，用户现有的 Gradle 修改留在原工作区。
- 真实账本的查询身份由账本服务验证。票据经 HTTPS 请求体传递，模型密钥由 Agent 服务端配置。
- 使用已有的模型规划、统计、会话记忆和 SSE 渲染流程。

## 模型与配置

- [x] 在 `tests/test_llm.py` 验证请求使用配置的 `/v1/chat/completions` 和 JSON 模式。
- [x] 验证旧 Ollama 环境变量仍可启动原生模式。
- [x] 更新 `app/core/config.py`、`app/core/llm.py`、`app/api/routes/health.py`，收口模型配置和就绪探测。
- [x] 运行 `python -m unittest discover -s tests -v` 并提交。

## 账本与 Agent 入口

- [x] 在账本后端验证票据一次性、有效期、无凭证拒绝和用户范围。
- [x] 新增账本票据签发、交换与受限查询 API，复用现有查询和 MongoDB。
- [x] 在 Agent 验证凭证缺失与浏览器身份字段拒绝；通过受限查询调用现有统计。
- [x] Vue 从同源会话获取状态，保留流式输出。移除 URL、JS 消息和家庭 ID 输入中的身份来源。
- [x] 运行两端 Python 测试和 `npm run build`，分别提交。

## Android

- [x] 验证配置地址仅能加载配置的 HTTPS 源。
- [x] 原生验证账号并申请票据，WebView POST 到 Agent。移除硬编码隧道和手机号查询参数。
- [x] 验证 Android 单元测试和 APK 构建，独立提交。

## 审查重点

- 旧安装包已缓存账号，首次打开 AI 账单需要重新验证密码。
- 过期或重复使用票据时提示重新进入。缺少环境配置时明确提示。
- SSE 建立前验证会话。家庭查询权限随账本后端的当前用户资料计算。
- 无账单和上游故障有不同状态。真实模型验证使用虚构输入。

## 部署联调

- [ ] 核对家庭主机、DNS、反代、账本部署和真实 MongoDB。
- [ ] 用目标域名在 Android 实机验证登录、查账与流式输出，并对照月报。
