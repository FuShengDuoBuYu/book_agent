# book_agent

用于自动记账的本地 Agent demo。

## 功能

- 提供一个 Vue 3 + Vite 的移动端聊天界面。
- 通过 FastAPI 暴露 `/api/chat`。
- 通过 LangChain 调用本地 Ollama 的 `qwen3:14b` 模型。
- 通过工具调用外部记账 API，并在本地做账单统计与总结。

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

## 新手学习顺序

如果你是第一次看 Agent 项目，不要一上来就扎进 `book_agent.py` 或 Prompt。更高效的顺序是先搞清楚“请求从哪进来、数据长什么样、谁负责做决策、谁负责执行工具、结果怎么回到前端”。

1. `README.md`
   先建立全局认识：项目是做什么的、怎么启动、前后端如何连起来。

2. `app/main.py`
   看应用是怎么启动的，理解 FastAPI 在哪里注册了路由、前端页面为什么也能被同一个服务托管。

3. `app/schemas/chat.py`
   先看聊天接口最外层的请求和响应结构，搞清楚前端到底传什么给后端，后端又返回什么。

4. `app/api/routes/chat.py`
   这层最适合初学者入门，因为它很薄。你要重点看两件事：
   一是普通接口和流式接口的区别；二是请求是怎么交给 `BookAgent` 处理的。

5. `app/agent/book_agent.py`
   这是后端主流程。读这一层时，不要纠结每个 helper 的细节，先抓主线：
   建立会话 -> 记录用户消息 -> 调 planner 生成计划 -> 执行工具 -> 让模型组织最终答案 -> 保存 assistant 回复。

6. `app/schemas/plan.py`
   在看 planner 之前，先看 `AgentPlan`、`ToolCall` 这些结构。这样你再看 planner 时，会知道它到底想产出什么。

7. `app/agent/planner.py`
   这一层是 Agent 和普通聊天机器人最不一样的地方。重点看：
   LLM 不是直接回答问题，而是先把问题转成结构化计划；然后代码再把这个计划补默认值、去重、纠正异常输出。

8. `app/agent/prompts.py`
   当你已经知道主流程和 plan 结构后，再看 prompt 才有意义。重点观察系统 prompt 给了模型哪些边界，以及最终回答阶段到底吃了哪些上下文。

9. `app/tools/personal_orders.py`
   这是“执行层”的代表文件。重点理解：tool schema 怎么定义、怎么调用外部账单 API、为什么有些筛选条件要在本地二次过滤。

10. `app/tools/order_calculations.py`
    这个文件代表“后处理层”。原始账单查回来后，并不是直接塞给模型，而是先做汇总、分类、Top N、对比分析，让模型站在结构化数据上回答。

11. `app/memory/store.py`
    这一层负责把会话和消息落到 MongoDB。重点理解 session 和 message 为什么分开存，以及 `sessionId` 在多轮对话里扮演什么角色。

12. `app/memory/mongo.py`
    这是更底层的资源接入层。你要看的是：Mongo client 在哪里创建，memory store 又是怎么拿到数据库句柄的。

13. `app/core/config.py` 和 `app/core/llm.py`
    这两个文件属于基础设施层。前者决定配置从哪里来，后者决定聊天模型和 planner 模型如何初始化、为什么要区分两种模型配置。

14. `app/clients/bookkeeping_api.py`
    当前外部账单查询的 HTTP 封装就在这里。你可以把它理解成“工具层依赖的下游客户端”。

15. `frontend/src/App.vue`
    最后再看前端主页面。重点看三件事：
    一是宿主 App 如何注入手机号；二是前端如何消费 SSE 流式事件；三是 `sessionId` 怎么缓存在本地并带回后端。

## 阅读时建议

- 第一遍只看主流程，不要陷进每个工具函数和每个 prompt 字眼里。
- 第二遍按“schema -> planner -> tool -> memory”重新串一次数据流。
- 如果你想学 Agent，最值得反复看的不是 UI，而是 `BookAgent`、`QueryPlanner` 和 tool/schema 之间怎么配合。
- 读到不理解的地方，优先问自己：这一层是在“决定做什么”，还是在“真正执行什么”。这个区分对理解 Agent 很重要。

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
