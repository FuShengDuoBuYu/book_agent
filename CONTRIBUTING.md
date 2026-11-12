# 开发规范

- 功能使用 `codex/` 分支或项目约定的功能分支。提交聚焦单个可验证的变化，标题沿用 `feat:`、`fix:`、`docs:`。提交前检查 `git status --short`、`git diff --check` 和暂存区。
- Agent 的模型配置集中在 `app/core`，授权请求集中在 `app/clients/bookkeeping_api.py`，Vue 使用同源 API。修改跨仓库契约时同时核对 Android、Agent 和账本后端。
- 金额和日期计算沿用服务端账本工具。改动统计口径时，用相同账本和区间对照 App 月报，覆盖涉及的退款、转账或时间边界。
- 变更行为时为具体回归加测试。提交 Agent 前运行 `python -m unittest discover -s tests -v` 与 `npm --prefix frontend run build`。账本后端使用自身的 unittest，Android 使用仓库的 Gradle Wrapper。
- 移除已由新入口替代的调用和配置前，查清调用方并用测试验证。测试使用虚构账单，环境变量、网关密钥和会话凭证保留在部署侧。
