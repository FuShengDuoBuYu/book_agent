# AI 账单 · book_agent

自动记账的账本对话服务。FastAPI 同源提供 Vue 聊天页和 SSE API，使用已验证的账本会话调用记账后端。Agent 先解析问题和追问指代，再查询账本、核算结果、检查证据是否足够，最后解释分析。支持按备注语义归并来源、家庭成员归属、表格与图表。

## 本地开发

使用 Python 3.11 和 Node.js 22 或更高版本。创建虚拟环境并激活后运行以下命令。

```sh
python -m pip install -r requirements.txt
npm --prefix frontend ci
npm --prefix frontend test
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

## 分析与历史对话

页面顶部的“历史对话”可回看、续聊和分页加载旧消息，“新对话”创建独立上下文。历史按服务端验证的账号及个人/家庭账本范围隔离。刷新页面自动恢复最近会话。

查询由授权账本接口执行，计算工具处理金额，模型组合证据作解释。支持以下可组合分析。

| 工具 | 用途 |
| --- | --- |
| `cashflow` | 收支结构、分类、支付方式、备注来源与结余比例 |
| `sources` | 脱敏成员、实际备注、来源目录与逐月贡献；供调查阶段归并同一项目 |
| `trends` / `changes` | 月度与每日趋势、最近完整月份变化及类别贡献 |
| `anomalies` | 同类异常大额和疑似重复账单线索 |
| `recurring` | 多月出现的收入来源和周期性支出 |
| `forecast` | 收入、支出、净现金流的条件预测 |
| `scenario` | 用户指定金额、次数与增减条件下的推演 |
| `projection` | 多月收支和累计新增结余；按来源调整金额或百分比 |
| `income_stability` / `runway` | 历史收入波动、来源集中度、给定余额和收入冲击下的现金缺口 |
| `optimization` / `purchase` | 保留必要支出的调整空间、大额购买后的现金及储备余量 |
| `data_quality` | 非持续性入账、缺失字段及重复线索对分析口径的影响 |
| `budget` / `savings` | 预算执行、剩余额度及储蓄目标测算 |

各分析绑定查询来源，避免分类范围与个人/家庭账本混用。未完月份单独标记；整月缺失不按零处理。收入预测在最近六个完整月内取最近三个有账单月份，逐来源计算月度贡献中位数，再加总为总额。某来源在有账单月份未出现，只说明当月没有记录贡献。来源归并和非持续性入账剔除保留备注依据，仍需核查；历史情景上下界不是置信区间。现有账单无法证明真实资产、未偿债务或未来工作安排。

`GET /api/conversations` 和 `GET /api/conversations/{id}` 提供授权分页历史。规划默认读取最近十二条用户与助手消息，追问沿用同范围的结构化条件、预测月份、来源分组及历史窗口，不把旧回答中的数字当作用户输入。独立问题、切换查询范围及明确重置会清除旧情景。

`analysisVersion=4` 的内部元数据记录 `understanding`（目标、指代、需回答的事项和证据需求）、原始计划、初次核算后的 `investigation`、最终证据、上下文、耗时及报告审查。历史 API 仅公开消息和校验过的 `artifacts`，不返回私有证据或内部元数据。表格、折线图和柱状图经过 SSE 发送并随历史恢复，可在支持下载的浏览器中导出 CSV 和 PNG；Android WebView 的文件下载需要单独在设备验证。

调查阶段读取实际备注目录和初次计算结果，可在已授权查询内补充只读分析并归并同义来源，再重新计算。分组只能引用已观察到的来源键，不能编造来源、混入其他账号或改变用户条件。为跨进程、跨重启保持脱敏成员键一致，部署时设置专用随机 `AGENT_MEMBER_KEY_SECRET`。

报告同时接收本轮目标、完整的回答要求和有界会话历史。数量使用证据变量引用，程序填入真实值；表格和图表直接由计算结果生成。无依据的数量会触发修正重试，程序不再静默删除整句。审查同时检查事实支持和任务完成情况。模型失败时保留相关核算结果、来源表格及真实缺口，并记录 `computed_fallback`；这仍不算解释质量验收通过。模型审查可能遗漏语义错误，需要持续做真实失败形态的回归。

## 模型回归

```sh
python -m scripts.evaluate_analysis
python -m scripts.evaluate_analysis --case scenario --case forecast
python -m scripts.evaluate_acceptance
python -m scripts.evaluate_acceptance --case chain
python -m scripts.evaluate_intent
python -m scripts.evaluate_intent --case forecast_followups --output intent-results.jsonl
```

上述命令使用配置中的模型，但账本及记忆由虚构样本替代，不访问用户数据库。输出 JSONL 包含问题类型、所选工具、数值检查和回答，可用于比较模型与提示词。程序检查不能替代对解释是否准确、是否把推断说成事实的人工复核。

`evaluate_intent` 针对信息利用和连续追问：同项目不同备注的语义归并、非默认目标月份保持、备注质疑后查证、来源到总额的计算方法、家庭同备注不同成员、支出变化、保护项和缺少余额时的购买判断。按实际交付内容验收，不只检查答案是否非空或包含金额。

`evaluate_acceptance` 覆盖八类分析问题、四轮连续追问及四个输入变体，共十六次回答；`--case variants` 可单独执行来源比例、购买储备、目标期限与全部停薪变体。合成样本的标准值和实际验收记录见 [分析质量回归](docs/analysis-quality-evaluation.md)。

原生 Ollama 规划使用 `AgentPlan` JSON Schema。`OLLAMA_NUM_CTX` 控制上下文长度，`PLANNER_NUM_PREDICT` 控制计划输出上限，`LLM_REASONING=true` 开启回答阶段推理；规划阶段关闭推理。按机器能力设置 `PLANNER_TIMEOUT_SECONDS` 和 `REQUEST_TIMEOUT_SECONDS`，并用上述回归验证后再发布。

复杂分析的结构化报告和审查使用零温度、关闭推理；普通聊天仍使用聊天配置。原生 Ollama 默认请求 8K 上下文，`OLLAMA_KEEP_ALIVE=60s` 表示请求完成后空闲一分钟释放模型。共享显卡时可设置 `OLLAMA_NUM_GPU` 限制 GPU 执行的层数，其他层由 CPU 执行；应实测显存余量和延迟，不把该值当作显存容量。对话元数据同时记录上下文、GPU 层数及驻留配置。

- [架构与部署](docs/ai-bill-architecture.md)
- [家庭部署信息清单](docs/home-agent-checklist.md)
- [开发规范](CONTRIBUTING.md)
