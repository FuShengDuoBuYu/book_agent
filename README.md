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
| `aggregate` | 按月份、年份、成员、来源、类别、支付方式、星期交叉分组；筛选、历史区间比较、笔数、每笔均额和中位数 |
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

各分析绑定查询来源，避免分类范围与个人/家庭账本混用。“最近两个完整月”等明确日期条件由程序按当前日历校验，排除尚未结束的本月。汇总同时记录实际查过的日期，区分查全、部分查询、未查询及查过但没有记录；未知金额不按零补齐。单次汇总查询跨度不超过 24 个自然月。跨期比较会先合并每期内的月份和年份，再对齐类别等维度。笔数与每笔均额的变化可作对称分解，不能据此推断商品单价、数量或现实原因。

收入预测默认在最近六个完整月内取最近三个有账单月份；用户指定历史样本区间时遵守该范围。逐来源计算月度贡献中位数，再加总为总额。某来源在有账单月份未出现，只说明当月没有记录贡献。来源归并和非持续性入账剔除保留备注依据，仍需核查；历史情景上下界不是置信区间。现有账单无法证明真实资产、未偿债务或未来工作安排。

`GET /api/conversations` 和 `GET /api/conversations/{id}` 提供授权分页历史。规划从最近十二条用户与助手消息中按上下文预算取用，明确条件独立保留。意图阶段区分独立问题、承接、纠正与重置；追问沿用同范围的结构化条件、预测月份、来源分组及历史窗口，不把旧回答中的数字当作用户输入。撤回某个假设只清除对应来源；明确的新期限和起始月份优先。

规划还可沿用上一轮完整、同账本与筛选范围的来源目录，仅投影来源名和收支方向，用于消解“家教少一半”等追问，不从旧助手措辞猜方向。同一简称对应多个来源或收支两向时保留歧义。模型为无约束筛选填写的 `all`、`不限` 等占位不会作为成员或类别名执行；用户明确引用同名类别时仍按字面筛选。用户已提供的价格、余额和目标需绑定到对应决策，不能再次当作未知条件询问。

`analysisVersion=4` 的内部元数据记录 `understanding`（目标、指代、需回答的事项和证据需求）、原始计划、初次核算后的 `investigation`、最终证据、上下文、耗时及报告审查。历史 API 仅公开消息和校验过的 `artifacts`，不返回私有证据或内部元数据。表格、折线图和柱状图经过 SSE 发送并随历史恢复，可在支持下载的浏览器中导出 CSV 和 PNG；Android WebView 的文件下载需要单独在设备验证。

调查阶段读取实际备注目录和初次计算结果，可在已授权查询内补充只读分析并归并同义来源，再重新计算。补查不会覆盖原分析目标。业务归组须有实际名称或备注中的共同依据，不能把同一人的独立工资、项目和出售款统称为一个来源；证据不足时保留原名分列。分组只能引用已观察到的来源键，不能编造来源、混入其他账号或改变用户条件。为跨进程、跨重启保持脱敏成员键一致，部署时设置专用随机 `AGENT_MEMBER_KEY_SECRET`。

报告分成提纲、固定证据段解释和整体校对。提纲先绑定本轮要回答的事项与事实编号，每段最多八条事实；程序先从已核算结果派生变化方向、目标是否可达、输入缺口及来源关系；小段写作只解释这些分析发现，不接收原始金额重新算数。标题和证据引用由代码填回。事实保留账本范围、历史窗口、指标及月均/月度合计等统计口径。遗漏的必需事实单独展示，不能补进无关解释段。每轮最多三个解释段，整体至多六段，最多修正一轮，模型调用按顺序执行。

金额、日期和数量由事实句及表格准确展示；解释阶段可选择本段已核算指标的数量引用，由程序填回金额、比例或月数，不重新猜算。叙述引用数量时仍校验本段的证据与单位。无依据的数量触发逐段纠错，程序不静默删除整句。纠错完整传回实际问题，不复制上一版错误解释。`validation.attempts[].compositionPlan` 保留提纲目标与引用，`validation.findings` 保留程序生成的分析发现，`calls.composition` 记录各步模型耗时和 token 数，便于区分意图、查证、计算和解释问题。整体审查同时检查实际显示的事实和目标完成情况。失败时保留相关计算结果并记录 `computed_fallback`，这不算解释质量验收通过。语义口径仍需要持续人工抽查。

## 模型回归

```sh
python -m scripts.check_model_contract
python -m scripts.evaluate_analysis
python -m scripts.evaluate_analysis --case scenario --case forecast
python -m scripts.evaluate_acceptance
python -m scripts.evaluate_acceptance --case chain
python -m scripts.evaluate_intent
python -m scripts.evaluate_intent --case forecast_followups --output intent-results.jsonl
```

上述命令使用配置中的模型，但账本及记忆由虚构样本替代，不访问用户数据库。输出 JSONL 包含问题类型、所选工具、数值检查和回答，可用于比较模型与提示词。程序检查不能替代对解释是否准确、是否把推断说成事实的人工复核。

修改模型或推理服务后，先运行 `check_model_contract`。它用没有账本数据的最小请求核对结构化输出是否真的受约束；只返回普通文字、字段或常量不匹配都算失败。`/ready` 列出模型并不能替代这个检查。家庭主机的 Ollama 0.21.2 已复现 Qwen3.5 在 `think=false` 时忽略格式约束的兼容问题，见 [上游修复](https://github.com/ollama/ollama/pull/15901)。固定运行时的验证和切换流程见架构文档。

`evaluate_intent` 针对信息利用和连续追问：同项目不同备注的语义归并、非默认目标月份保持、备注质疑后查证、来源到总额的计算方法、家庭同备注不同成员、支出变化、保护项和缺少余额时的购买判断，以及筛选后跨期分类统计、承接日期范围的星期分组。按实际交付内容验收，不只检查答案是否非空或包含金额。两月样本与默认三月基准分别核算，不把改变时间口径当作同一组标准答案。

`evaluate_acceptance` 覆盖八类分析问题、四轮连续追问及四个输入变体，共十六次回答；`--case variants` 可单独执行来源比例、购买储备、目标期限与全部停薪变体。合成样本的标准值和实际验收记录见 [分析质量回归](docs/analysis-quality-evaluation.md)。

原生 Ollama 先使用较小的意图 Schema 理解目标、指代和交付项，再使用 `AgentPlan` JSON Schema 生成执行计划。默认 `LLM_INTENT_REASONING=false`、`LLM_PLANNING_REASONING=false`：实测这台机器的 9B 模型开启深度推理会耗尽输出长度而不返回计划。保留分阶段结构化理解与计划核对，避免依赖无界推理。`PLANNER_NUM_PREDICT=2048` 限制计划输出，`OLLAMA_NUM_CTX` 控制上下文长度。隐藏推理不展示或落库；记录的是目标理解、执行参数和阶段耗时。规划、报告和重试分别保留当前目标及明确条件，再按预算压缩历史与证据预览。报告写作和校对关闭推理，使用独立输出预算。按机器能力设置 `PLANNER_TIMEOUT_SECONDS` 和 `REQUEST_TIMEOUT_SECONDS`，并用上述回归验证后再发布。

复杂分析的结构化报告和审查使用零温度、关闭推理；普通聊天仍使用聊天配置。原生 Ollama 默认请求 8K 上下文，`OLLAMA_KEEP_ALIVE=60s` 表示请求完成后空闲一分钟释放模型。共享显卡时可设置 `OLLAMA_NUM_GPU` 限制 GPU 执行的层数，其他层由 CPU 执行；应实测显存余量和延迟，不把该值当作显存容量。对话元数据同时记录上下文、GPU 层数及驻留配置。

- [架构与部署](docs/ai-bill-architecture.md)
- [家庭部署信息清单](docs/home-agent-checklist.md)
- [开发规范](CONTRIBUTING.md)
