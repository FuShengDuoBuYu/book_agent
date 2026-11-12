# AI 账单 · book_agent

自动记账的账本对话服务。FastAPI 同源提供 Vue 聊天页和 SSE API，使用已验证的账本会话调用记账后端。Agent 规划查询，代码统计金额，模型解释结果。

## 本地开发

使用 Python 3.11 和 Node.js 22 或更高版本。创建虚拟环境并激活后运行以下命令。

```sh
python -m pip install -r requirements.txt
npm --prefix frontend ci
npm --prefix frontend run build
python -m unittest discover -s tests -v
```

复制 `.env.example` 为 `.env`，填写记账后端、聊天记忆 MongoDB 和模型网关配置。然后启动服务。

```sh
python -m uvicorn app.main:app --host 127.0.0.1 --port 8001
```

- `GET /health` 检查应用进程。
- `GET /ready` 检查聊天记忆配置和网关是否列出所选模型。
- `GET /` 提供 WebView 页面。从 Android 进入时，原生端使用一次性票据建立 WebView 会话。
- `npm --prefix frontend run dev` 启动 Vue 开发服务器。Vite 代理 `/api` 和 `/health`。

`LLM_PROVIDER=openai_compatible` 使用配置的兼容网关。原生 Ollama 可用 `LLM_PROVIDER=ollama`、`LLM_BASE_URL=http://127.0.0.1:11434` 和 `LLM_MODEL`；已有的 `OLLAMA_BASE_URL`、`OLLAMA_MODEL` 等仍可作为回退配置。

- [架构与部署](docs/ai-bill-architecture.md)
- [家庭部署信息清单](docs/home-agent-checklist.md)
- [开发规范](CONTRIBUTING.md)
