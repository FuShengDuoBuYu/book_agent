# book_agent

用于自动记账的本地 Agent demo。

## 功能

- 提供一个 Vue 3 + Vite 的移动端聊天界面。
- 通过 FastAPI 暴露 `/api/chat`。
- 通过 LangChain 调用本地 Ollama 的 `qwen3:14b` 模型。
- 当前 demo 不读取真实账单 API，只演示对话体验。

## 后端结构

```text
app/
  api/routes/        FastAPI 路由
  agent/             LangChain Agent、prompt、编排逻辑
  clients/           外部 API 客户端，后续放云端账单接口
  core/              配置、LLM 初始化等基础设施
  schemas/           请求/响应模型
  services/          普通业务服务
  tools/             LangChain tools，后续放账单查询/统计工具
main.py              uvicorn 兼容入口
```

## 准备 Ollama

先确认本地已经安装 Ollama，并拉取模型：

```bash
ollama run qwen3:14b
```

如果想临时使用其他模型：

```bash
export OLLAMA_MODEL=qwen3:8b
```

## 配置 MongoDB 记忆

Agent 使用 MongoDB Atlas 保存会话和历史消息。连接串不要写进代码，启动前设置环境变量：

```bash
export AGENT_MONGODB_URI='mongodb+srv://用户名:密码@你的集群.mongodb.net/?appName=AutoBookKeeping'
export AGENT_MONGODB_DB='book_agent'
```

如果更换过 Atlas 密码，记得同步更新 `AGENT_MONGODB_URI`。

## 启动服务

安装后端依赖：

```bash
pip install -r requirements.txt
```

启动后端 Agent：

```bash
uvicorn main:app --reload --host 0.0.0.0 --port 8001
```

安装前端依赖：

```bash
cd frontend
npm install
```

开发模式启动前端：

```bash
npm run dev
```

浏览器打开 Vite 地址：

```text
http://localhost:5173
```

Vite 会把 `/api` 和 `/health` 代理到后端的 `http://localhost:8001`。

## WebView 构建

如果要让 FastAPI 直接服务前端页面：

```bash
cd frontend
npm run build
```

然后访问：

```text
http://localhost:8001
```

健康检查：

```text
http://localhost:8001/health
```

## WebView 建议

当前前端已经迁移到 Vue 3 + Vite。移动端 WebView 建议加载构建后的后端地址 `http://你的服务地址:8001`，页面只保留对话能力，不包含拍照、图片、文件、语音等多模态入口。

手机号不由用户在网页中输入，而是由 App 注入。当前网页支持以下几种注入方式，后续 App 端任选一种即可：

```text
http://localhost:5173?phoneNum=13800138000
```

或在 WebView 加载前注入：

```js
window.__BOOK_AGENT_CONTEXT__ = { phoneNum: "13800138000" };
```

或在页面加载后调用：

```js
window.setBookAgentContext({ phoneNum: "13800138000" });
window.setBookAgentPhoneNum("13800138000");
```

也可以向 WebView 发送消息：

```js
window.postMessage({
  type: "BOOK_AGENT_CONTEXT",
  phoneNum: "13800138000",
});
```

前端会按手机号在 `localStorage` 保存当前 `sessionId`，后端会用 `phoneNum + sessionId` 从 MongoDB 读取最近几轮会话历史。
