# 开发规范

- 功能使用 `codex/` 分支或项目约定的功能分支。提交聚焦单个可验证的变化，标题沿用 `feat:`、`fix:`、`docs:`。提交前检查 `git status --short`、`git diff --check` 和暂存区。
- Agent 的模型配置集中在 `app/core`，授权请求集中在 `app/clients/bookkeeping_api.py`，Vue 使用同源 API。修改跨仓库契约时同时核对 Android、Agent 和账本后端。
- 金额和日期计算沿用服务端账本工具。改动统计口径时，用相同账本和区间对照 App 月报，覆盖涉及的退款、转账或时间边界。
- 变更行为时先写具体回归并确认失败，再修改实现。提交 Agent 前运行 `python -m unittest discover -s tests -v`、`npm --prefix frontend test` 与 `npm --prefix frontend run build`。账本后端使用自身的 unittest，Android 使用仓库的 Gradle Wrapper。
- 意图、规划和信息利用的改动必须运行 `scripts.evaluate_intent` 的相关真实模型回归，并检查多轮指代、原始备注、来源归并、查询范围、条件继承和实际交付内容。记录模型、配置、修订版本及失败原因；禁止把 CI 通过当成语义质量通过。
- 新分析输出由已核算证据构造，数值需可追溯并能核对总额。新工具必须进入统一注册表，调查阶段仅可补充已授权快照内的只读分析。旧逻辑替换后移除冗余分支，不能靠静默删句隐藏失败。
- 移除已由新入口替代的调用和配置前，查清调用方并用测试验证。测试使用虚构账单，环境变量、网关密钥和会话凭证保留在部署侧。
