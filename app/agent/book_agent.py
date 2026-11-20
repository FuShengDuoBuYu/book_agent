import json
import re
import logging
from collections import defaultdict
from collections.abc import AsyncIterator
from contextlib import aclosing
from copy import deepcopy
from datetime import datetime
from time import perf_counter
from typing import Any
from zoneinfo import ZoneInfo

from langchain_core.prompts import ChatPromptTemplate
from langsmith import traceable

from app.agent.planner import HISTORY_BASED_TOOLS, QueryPlanner
from app.agent.analysis_context import latest_context, logical_queries
from app.agent.evidence_report import EvidenceReporter, money
from app.agent.report_values import literal
from app.agent.artifacts import build_artifacts, render_artifacts_markdown
from app.agent.investigation import EvidenceInvestigator, merged_source_groups
from app.agent.source_grouping import grounded_source_groups
from app.agent.progress import with_progress
from app.agent.prompts import BOOK_AGENT_ANALYSIS_PROMPT, BOOK_AGENT_SYSTEM_PROMPT
from app.clients.bookkeeping_api import BookkeepingApiClient
from app.core.llm import get_chat_model
from app.core.config import get_settings
from app.core.model_usage import TurnUsage, capture_usage, current_usage
from app.memory.store import MongoMemoryStore, conversation_scope
from app.schemas.plan import AgentPlan, AnalysisRequest, ToolCall
from app.tools.order_calculations import (
    calculate_category_breakdown,
    calculate_order_summary,
    compare_periods,
    find_top_expenses,
    find_top_incomes,
)
from app.tools.order_search import search_orders
from app.tools.registry import PLANNER_TOOL_BY_NAME, format_tool_call
from app.tools.financial_analysis import _date, merge_orders, run_analysis
from app.agent.evidence_pipeline import collect_analysis_results

logger = logging.getLogger(__name__)


class PlanningUnavailableError(RuntimeError):
    """Structured planning exhausted its bounded retries without a usable plan."""


class BookAgent:
    def __init__(self, ledger_client: BookkeepingApiClient) -> None:
        self.ledger_client = ledger_client
        # BookAgent 是总编排器：接用户输入、调 planner、调工具、再把结果交给模型生成答案。
        self.prompt = ChatPromptTemplate.from_messages(
            [
                ("system", BOOK_AGENT_SYSTEM_PROMPT),
                (
                    "human",
                    BOOK_AGENT_ANALYSIS_PROMPT,
                ),
            ]
        )
        self.model = get_chat_model()
        self.chain = self.prompt | self.model
        self.memory = MongoMemoryStore()
        self.planner = QueryPlanner()
        self.reporter = EvidenceReporter()
        self.investigator = EvidenceInvestigator()

    async def chat(
        self,
        phone_num: str,
        message: str,
        session_id: str | None = None,
        mode: str = "个人版",
        family_id: str | None = None,
        new_session: bool = False,
    ) -> tuple[str, str | None]:
        chunks: list[str] = []
        requested_artifacts = []
        resolved_session_id: str | None = None
        async for event in self.stream_chat(
            phone_num=phone_num,
            message=message,
            session_id=session_id,
            mode=mode,
            family_id=family_id,
            new_session=new_session,
        ):
            if event["type"] == "session":
                resolved_session_id = event["sessionId"]
            if event["type"] == "delta":
                chunks.append(event["content"])
            elif event["type"] == "artifacts":
                requested_artifacts = [item for item in event["artifacts"] if item.get("presentation") == "requested"]
            elif event["type"] == "error":
                raise RuntimeError(event["content"])

        reply = "".join(chunks).strip()
        table_text = render_artifacts_markdown(requested_artifacts)
        return reply + ("\n\n" + table_text if table_text else ""), resolved_session_id

    async def stream_chat(
        self,
        phone_num: str,
        message: str,
        session_id: str | None = None,
        mode: str = "个人版",
        family_id: str | None = None,
        new_session: bool = False,
    ) -> AsyncIterator[dict[str, str]]:
        usage = TurnUsage()
        stream = self._stream_chat(
            phone_num=phone_num, message=message, session_id=session_id,
            mode=mode, family_id=family_id, new_session=new_session,
        )
        try:
            while True:
                # A consumer can resume or close this generator in another
                # task. Bind and reset within each advance, before yielding.
                with capture_usage(usage):
                    try:
                        event = await anext(stream)
                    except StopAsyncIteration:
                        return
                yield event
        finally:
            with capture_usage(usage):
                await stream.aclose()

    async def _stream_chat(
        self,
        phone_num: str,
        message: str,
        session_id: str | None = None,
        mode: str = "个人版",
        family_id: str | None = None,
        new_session: bool = False,
    ) -> AsyncIterator[dict[str, str]]:
        # stream_chat 是整个 Agent 的主流程。
        # 前端看到的 status / thinking / delta / done 事件，都是从这里连续产出的。
        resolved_session_id: str | None = None
        assistant_chunks: list[str] = []
        user_message_saved = False
        confirmed_context: dict[str, Any] = {}
        started_at = perf_counter()
        try:
            book_mode = self._normalize_mode(mode)
            family_id = str(family_id or "").strip()
            book_scope = conversation_scope(book_mode, family_id)
            yield {"type": "status", "content": "正在解析你的问题"}
            yield {"type": "status", "content": f"当前账本模式：{book_mode}"}
            resolved_session_id = await self.memory.ensure_session(
                phone_num=phone_num,
                session_id=session_id,
                first_message=message,
                book_scope=book_scope,
                new_session=new_session,
            )
            yield {"type": "session", "sessionId": resolved_session_id}
            history_messages = await self.memory.get_recent_messages(
                phone_num=phone_num,
                session_id=resolved_session_id,
                limit=12,
            )
            confirmed_context = latest_context(history_messages)
            yield {
                "type": "status",
                "content": f"已加载 {len(history_messages)} 条会话历史",
            }
            history_text = self._format_history(history_messages)
            await self.memory.add_message(
                phone_num=phone_num,
                session_id=resolved_session_id,
                role="user",
                content=message,
            )
            user_message_saved = True

            yield {"type": "status", "content": "正在规划账单查询"}
            async with aclosing(with_progress(self._create_plan(
                message=message, history=history_text, book_mode=book_mode,
                previous_context=deepcopy(confirmed_context),
            ), '正在理解问题与已有会话')) as progress_stream:
                async for progress in progress_stream:
                    if progress['type'] == 'result':
                        plan = progress['value']
                    else:
                        yield progress
            if (plan.intent == 'unknown' and not plan.needsTools and not plan.analyses
                    and any(issue.get('type') in {'TimeoutError', 'JSONDecodeError', 'ValidationError', 'ValueError'}
                            for issue in plan._planning_issues)):
                # A failed planner is not a completed financial turn. Route it
                # through the same audited failure path as a provider error so
                # the last confirmed context remains available for a retry.
                raise PlanningUnavailableError()
            yield {
                "type": "status",
                "content": f"查询计划：{plan.summary}",
            }

            if plan.needsTools:
                for tool_call in plan.toolCalls:
                    yield {
                        "type": "status",
                        "content": (
                            f"准备调用工具：{tool_call.id}，"
                            f"{format_tool_call(tool_call)}，"
                            f"分类={tool_call.args.cost_type or '不限'}"
                        ),
                    }
                async with aclosing(with_progress(self._execute_tool_calls(
                    mode=book_mode,
                    family_id=family_id,
                    plan=plan,
                    question=message, history=history_text,
                ), '正在查证备注、来源与分析依据')) as progress_stream:
                    async for progress in progress_stream:
                        if progress['type'] == 'result':
                            orders_json = progress['value']
                        else:
                            yield progress
                yield {"type": "status", "content": self._query_status(orders_json)}
            else:
                # 不需要查账单时，也统一给后续模型一个结构稳定的空结果。
                orders_json = json.dumps(
                    {
                        "ok": True,
                        "message": "本轮无需查询账单。",
                        "orderCount": 0,
                    },
                    ensure_ascii=False,
                )
            self._assert_no_raw_orders_for_answer(orders_json)
            yield {"type": "status", "content": "正在生成分析结果"}

            deterministic_reply = self._build_deterministic_reply(
                plan=plan,
                orders_json=orders_json,
            )
            if not deterministic_reply and not plan.analyses and (plan.needsTools or plan.intent != 'chat'):
                deterministic_reply = self._computed_query_reply(self._safe_json_loads(orders_json))
            if deterministic_reply:
                assistant_chunks.append(deterministic_reply)
                yield {"type": "delta", "content": deterministic_reply}
                await self._save_assistant_reply(
                    phone_num=phone_num,
                    session_id=resolved_session_id,
                    reply=deterministic_reply,
                    plan=plan,
                    evidence=self._safe_json_loads(orders_json),
                    duration_ms=round((perf_counter() - started_at) * 1000),
                    validation={'status': 'grounded', 'mechanism': 'computed_query_summary',
                                'compositionStatus': 'fallback'},
                )
                yield {"type": "done", "content": ""}
                return

            if plan.analyses:
                results = self._safe_json_loads(orders_json).get('analysisResults', [])
                needs = set(plan.understanding.evidenceNeeds)
                primary_tools = {row['tool'] for row in results if not row.get('supplemental')}
                chart_target = next((tool for tool in ('projection', 'forecast', 'aggregate', 'trends')
                                     if tool in primary_tools), None)
                output = {**plan.output.model_dump(), 'chartTarget': chart_target,
                          'includeLineage': 'remarks' in needs or plan.output.lineage,
                          'lineageRequested': plan.output.lineage,
                          'includeSources': bool(needs & {'sources', 'members', 'remarks'} or
                             ((plan.output.table or plan.output.export) and not needs & {'trends', 'changes', 'forecast'})),
                          'includeTrends': (plan.output.chart != 'none' and chart_target == 'trends') or
                             ((plan.output.table or plan.output.export) and 'trends' in needs)}
                artifacts = build_artifacts(results, output,
                    window=plan.analysisWindow.model_dump(mode='json') if plan.analysisWindow else None)
                async with aclosing(with_progress(self.reporter.answer(
                    message, plan._analysis_context, results, history=history_text,
                    instruction=plan.finalInstruction, artifacts=artifacts,
                    understanding=plan.understanding.model_dump(), analysis_scope={
                        'historyWindow': plan.analysisWindow.model_dump(mode='json') if plan.analysisWindow else None,
                        'asOfDate': datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat(),
                        'primaryTasks': [task.model_dump(mode='json', exclude_none=True) for task in plan.analyses if not task._supplemental],
                        'sourceGroups': [group.model_dump(mode='json') for group in plan.sourceGroups]
                    }), '正在核对分析结论与问题要求')) as progress_stream:
                    async for progress in progress_stream:
                        if progress['type'] == 'result':
                            reply, validation = progress['value']
                        else:
                            yield progress
                assistant_chunks.append(reply)
                yield {'type': 'delta', 'content': reply}
                if artifacts:
                    yield {'type': 'artifacts', 'artifacts': artifacts}
                await self._save_assistant_reply(
                    phone_num=phone_num, session_id=resolved_session_id, reply=reply, plan=plan,
                    evidence=self._safe_json_loads(orders_json),
                    duration_ms=round((perf_counter() - started_at) * 1000), validation=validation, artifacts=artifacts)
                yield {'type': 'done', 'content': ''}
                return

            # Only an explicit non-ledger chat may use a free-form response.
            think_parser = ThinkTagStreamParser()
            async for chunk in self.chain.astream(
                {
                    "message": message,
                    "history": history_text,
                    "query_summary": plan.summary,
                    "plan_json": json.dumps({"analyses": [task.model_dump() for task in plan.analyses],
                                             "analysisWindow": plan.analysisWindow.model_dump(mode="json") if plan.analysisWindow else None},
                                            ensure_ascii=False) if plan.analyses else plan.model_dump_json(),
                    "orders_json": self._answer_evidence(orders_json),
                }
            ):
                content = getattr(chunk, "content", "")
                if content:
                    for event in think_parser.feed(content):
                        if event["type"] == "delta":
                            assistant_chunks.append(event["content"])
                        yield event

            for event in think_parser.flush():
                if event["type"] == "delta":
                    assistant_chunks.append(event["content"])
                yield event

            assistant_reply = "".join(assistant_chunks).strip()
            await self._save_assistant_reply(
                phone_num=phone_num,
                session_id=resolved_session_id,
                reply=assistant_reply,
                plan=plan,
                evidence=self._safe_json_loads(orders_json),
                duration_ms=round((perf_counter() - started_at) * 1000),
            )

            yield {"type": "done", "content": ""}
        except Exception as exc:
            error_type = self._safe_error_type(exc)
            logger.warning("AI bill chat failed (%s)", error_type)
            failure_message = "AI 账单处理失败，请稍后重试。"
            if resolved_session_id and user_message_saved:
                try:
                    await self.memory.add_message(
                        phone_num=phone_num, session_id=resolved_session_id,
                        role='assistant', content=failure_message,
                        metadata={
                            'status': 'error', 'errorType': error_type,
                            'releaseRevision': get_settings().release_revision,
                            'durationMs': round(max(0, perf_counter() - started_at) * 1000),
                            'modelUsage': (current_usage() or TurnUsage()).snapshot(),
                            'analysisContext': deepcopy(confirmed_context),
                            'contextOrigin': 'prior_confirmed_turn' if confirmed_context else None,
                        },
                    )
                except Exception as storage_exc:
                    logger.warning('AI bill failure history save failed (%s)', self._safe_error_type(storage_exc))
            yield {"type": "error", "content": failure_message}

    @staticmethod
    def _safe_error_type(error: BaseException) -> str:
        return re.sub(r'[^A-Za-z0-9_]', '', type(error).__name__)[:128] or 'Error'

    def _query_status(self, orders_json: str) -> str:
        data = self._safe_json_loads(orders_json)
        results = data.get("toolResults") or []
        if any(not result.get("ok") for result in results):
            return "账单查询未完成"
        return f"查询完成，共获取 {data.get('totalOrderCount', 0)} 条账单"

    def _answer_evidence(self, orders_json: str) -> str:
        data = self._safe_json_loads(orders_json)
        if data.get("analysisResults"):
            # Present one coherent analysis, without duplicate per-query amounts.
            data = {"analysisResults": data["analysisResults"]}
        return json.dumps(data, ensure_ascii=False, default=str)

    @staticmethod
    def _computed_query_reply(data):
        """Keep legacy query plans useful without sending finance to a free writer."""
        rows = data.get('toolResults') or []
        if not data.get('ok') or not rows:
            return '本轮未得到可核算的账本证据，请明确查询范围后重试。'
        paragraphs = []
        for result in rows:
            calculated = result.get('calculationToolResults') or {}
            totals = calculated.get('summary') or {}
            heading = literal(result.get('mode') or '当前账本') + ' ' + literal(result.get('summary') or '所选查询')
            if not result.get('ok') or not totals:
                paragraphs.append(heading + '：未取得完整统计，金额未知。')
                continue
            paragraphs.append(heading + f"：已记 {totals['orderCount']} 笔，收入 {money(totals['incomeTotal'])} 元，"
                + f"支出 {money(totals['expenseTotal'])} 元，净额 {money(totals['netIncomeMinusExpense'])} 元。")
            for key, label in [('expenseCategories', '支出类别'), ('incomeCategories', '收入类别')]:
                for item in calculated.get('categoryBreakdown', {}).get(key, []):
                    paragraphs.append(label + literal(item['name']) + f"：{money(item['amount'])} 元，{item['orderCount']} 笔。")
            for key, label in [('topExpenses', '支出记录'), ('topIncomes', '收入记录')]:
                for item in calculated.get(key, {}).get('orders', []):
                    paragraphs.append(label + literal(item['date']) + ' ' + literal(item.get('costType') or '未分类')
                        + f"：{money(item['amount'])} 元；备注：" + literal(item.get('orderRemark') or '无备注'))
        if len(rows) > 1:
            paragraphs.append('各查询分别统计，范围重叠时不能直接相加。')
        paragraphs.append('以上为所选范围的已记收支，净额不能代替当前可动用余额，也不能证明未来安排。')
        return '\n\n'.join(paragraphs)

    async def _save_assistant_reply(
        self,
        phone_num: str,
        session_id: str | None,
        reply: str,
        plan: AgentPlan,
        evidence: dict | None = None,
        duration_ms: int | None = None,
        validation: dict | None = None,
        artifacts: list | None = None,
    ) -> None:
        if not reply or not session_id:
            return
        settings = get_settings()
        primary = [task.model_dump(mode='json', exclude_none=True) for task in plan.analyses if not task._supplemental]
        active = [task for task in primary if task['tool'] != 'sources']
        prior_active = plan._analysis_context.get('activeAnalyses', [])
        current_window = plan.analysisWindow.model_dump(mode='json') if plan.analysisWindow else None
        intent = ' '.join([plan.understanding.objective, *plan.understanding.requestedAnswers])
        tracing_parent = (plan.followUpOfPreviousQuestion and not plan.resetAnalysisContext
            and any(row['tool'] in HISTORY_BASED_TOOLS | {'budget'} for row in prior_active)
            and not any(row['tool'] in HISTORY_BASED_TOOLS | {'budget'} for row in primary)
            and current_window == plan._analysis_context.get('activeHistoryWindow')
            and bool(re.search(r'备注|归并|归组|归类|依据|参照|怎么.*(?:算|来)|计算方法', intent)))
        if active and not tracing_parent:
            plan._analysis_context['activeAnalyses'] = active
            plan._analysis_context['activeHistoryWindow'] = current_window
        plan._analysis_context['conversation'] = {
            'objective': plan.understanding.objective,
            'analyses': primary,
            'queries': logical_queries(plan),
            'analysisWindow': plan.analysisWindow.model_dump(mode='json') if plan.analysisWindow else None,
            'output': plan.output.model_dump(),
            'sourceGroups': [group.model_dump() for group in plan.sourceGroups],
        }
        await self.memory.add_message(
            phone_num=phone_num,
            session_id=session_id,
            role="assistant",
            content=reply,
            metadata={"plan": plan.model_dump(mode="json"), "evidence": evidence or {},
                      "analysisVersion": 6, "releaseRevision": settings.release_revision, "durationMs": duration_ms,
                      "artifacts": artifacts or [], "understanding": plan.understanding.model_dump(),
                      "investigation": plan._investigation,
                      "planningReview": plan._semantic_review,
                      "analysisContext": plan._analysis_context, "validation": validation or {},
                      "planningIssues": plan._planning_issues,
                      "modelUsage": (current_usage() or TurnUsage()).snapshot(),
                      "model": {"provider": settings.llm_provider, "name": settings.llm_model,
                                "reportMechanism": "cited_conclusions",
                                "planningTemperature": None if settings.llm_provider == 'openai' else 0,
                                "api": 'responses' if settings.llm_provider == 'openai' else
                                    'chat_completions' if settings.llm_provider == 'openai_compatible' else 'ollama',
                                "reasoning": settings.llm_reasoning,
                                "planningReasoning": settings.planner_reasoning,
                                "intentReasoning": settings.intent_reasoning,
                                "reasoningEffort": settings.llm_reasoning_effort,
                                "contextLength": settings.ollama_num_ctx,
                                "gpuLayers": settings.ollama_num_gpu,
                                "keepAlive": settings.ollama_keep_alive}},
        )

    @traceable(name="book_agent_create_plan", run_type="chain")
    async def _create_plan(
        self, message: str, history: str, book_mode: str, previous_context: dict | None = None
    ) -> AgentPlan:
        return await self.planner.create_plan(
            message=message,
            history=history,
            book_mode=book_mode,
            previous_context=previous_context,
        )

    @traceable(
        name="book_agent_execute_tool_calls",
        run_type="chain",
        process_inputs=lambda inputs: BookAgent._mask_trace_inputs(inputs),
    )
    async def _execute_tool_calls(
        self,
        mode: str,
        family_id: str | None,
        plan: AgentPlan,
        question: str = '',
        history: str = '',
    ) -> str:
        # 一个 plan 里可能包含多个 tool call，例如“对比上月和本月”会产生两次查询。
        tool_results = []
        datasets = defaultdict(list)
        total_order_count = 0
        book_mode = self._normalize_mode(mode)
        family_id = str(family_id or "").strip()

        for tool_call in plan.toolCalls:
            parsed_result = await self._execute_tool_call(tool_call, family_id)
            if parsed_result.get("ok") and plan.analysisWindow and not plan.analyses:
                window = plan.analysisWindow
                records = [order for order in parsed_result.get("ordersInfo", [])
                           if _date(order) and window.startDate <= _date(order) <= window.endDate]
                parsed_result = {**parsed_result, "ordersInfo": records, "orderCount": len(records)}
            if parsed_result.get("ok"):
                datasets[parsed_result.get("mode", book_mode)].append((tool_call, parsed_result.get("ordersInfo", [])))
            order_count = int(parsed_result.get("orderCount") or 0)
            total_order_count += order_count
            # 查询工具返回原始账单，统计工具再把原始账单加工成适合回答的摘要信息。
            calculation_tool_results = (
                self._calculate_orders(parsed_result, plan) if parsed_result.get("ok") else {}
            )
            tool_results.append(
                {
                    "toolCallId": tool_call.id,
                    "toolName": tool_call.toolName,
                    "reason": tool_call.reason,
                    "args": tool_call.args.model_dump(mode="json"),
                    "summary": format_tool_call(tool_call),
                    "mode": parsed_result.get("mode", book_mode),
                    "ok": parsed_result.get("ok", False),
                    "error": parsed_result.get("error"),
                    "message": parsed_result.get("message"),
                    "orderCount": order_count,
                    "userFound": bool(parsed_result.get("userFound")),
                    "query": parsed_result.get("query", {}),
                    "localFilter": parsed_result.get("localFilter", {}),
                    "normalizedCostType": parsed_result.get("normalizedCostType", "不限"),
                    "calculationToolResults": calculation_tool_results,
                }
            )

        comparison_tool_result = None
        if len(tool_results) > 1 and all(item["ok"] for item in tool_results):
            comparison_tool_result = self._compare_tool_results(tool_results)

        analysis_results = []
        if plan.analyses and all(item["ok"] for item in tool_results):
            today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
            catalogs = []
            for scope, queries in datasets.items():
                records = merge_orders([rows for _, rows in queries])
                # Reflection sees actual observed labels/remarks, never the raw account IDs.
                catalogs.append({'mode': scope, **run_analysis('sources', records, as_of=today, catalog_metric=plan.output.metric)})
            plan.sourceGroups, grouping_issues = grounded_source_groups(plan.sourceGroups, catalogs, question)
            initial_results = collect_analysis_results(plan, datasets, today)
            if question and getattr(self, 'investigator', None):
                decision, plan._investigation = await self.investigator.refine(question, history, plan, catalogs, results=initial_results)
                plan._investigation.setdefault('issues', [])[:0] = grouping_issues
                if decision.sourceGroups:
                    plan.sourceGroups = merged_source_groups(plan.sourceGroups, decision.sourceGroups)
                    plan.sourceGroups, final_grouping_issues = grounded_source_groups(plan.sourceGroups, catalogs, question)
                    plan._investigation.setdefault('issues', []).extend(final_grouping_issues)
                for task in decision.additionalAnalyses[:max(0, 12 - len(plan.analyses))]:
                    supplemental = AnalysisRequest.model_validate(task.model_dump())
                    supplemental._supplemental = True
                    plan.analyses.append(supplemental)
                # Reflection selects tools and source identities only. Its free-text
                # guesses must never become authoritative amounts or instructions.
            analysis_results = collect_analysis_results(plan, datasets, today)

        return json.dumps(
            {
                "ok": True,
                "message": "账单查询完成。",
                "planSummary": plan.summary,
                "analysisType": plan.analysisType,
                "mode": book_mode,
                "familyIdSet": bool(family_id),
                "finalInstruction": plan.finalInstruction,
                "totalOrderCount": total_order_count,
                "toolResults": tool_results,
                "comparisonToolResult": comparison_tool_result,
                "analysisResults": analysis_results,
            },
            ensure_ascii=False,
            default=str,
        )

    async def _execute_tool_call(self, tool_call: ToolCall, family_id: str) -> dict:
        tool = PLANNER_TOOL_BY_NAME.get(tool_call.toolName)
        if tool is None:
            return {"ok": False, "error": "unsupported_tool"}

        mode, _ = tool
        family_scope = family_id if mode == "家庭版" else ""
        if mode == "家庭版" and not family_scope:
            return {"ok": False, "error": "family_not_available"}

        return await search_orders(
            self.ledger_client, mode, family_scope=family_scope, **tool_call.args.model_dump()
        )

    def _calculate_orders(
        self, parsed_result: dict[str, Any], plan: AgentPlan
    ) -> dict[str, Any]:
        orders_json = json.dumps(parsed_result, ensure_ascii=False, default=str)
        analysis_type = plan.analysisType
        results: dict[str, Any] = {
            "summary": self._safe_json_loads(calculate_order_summary(orders_json))
        }

        if analysis_type in {"category_expense", "comparison", "general_summary"}:
            results["categoryBreakdown"] = self._safe_json_loads(
                calculate_category_breakdown(orders_json)
            )

        if analysis_type in {"highest_expense", "comparison", "general_summary"}:
            results["topExpenses"] = self._safe_json_loads(
                find_top_expenses(orders_json, top_n=5)
            )

        if analysis_type in {"income_expense", "general_summary"}:
            results["topIncomes"] = self._safe_json_loads(
                find_top_incomes(orders_json, top_n=5)
            )

        return results

    def _compare_tool_results(
        self, tool_results: list[dict[str, Any]]
    ) -> dict[str, Any]:
        periods = [
            {
                "label": item.get("summary", ""),
                "calculationToolResults": item.get("calculationToolResults", {}),
            }
            for item in tool_results
        ]
        raw_comparison = compare_periods(
            json.dumps(periods, ensure_ascii=False, default=str)
        )
        return self._safe_json_loads(raw_comparison)

    def _build_deterministic_reply(
        self,
        plan: AgentPlan,
        orders_json: str,
    ) -> str | None:
        data = self._safe_json_loads(orders_json)
        if plan.intent == "unknown":
            return "这次未能生成可执行的分析计划，请补充你想分析的范围或换一种表述后重试。"
        if not data.get("ok", False):
            return None
        if not plan.needsTools:
            return None

        tool_results = data.get("toolResults") or []
        if not tool_results:
            return None
        if any(item.get("error") == "family_not_available" for item in tool_results):
            return "当前账号未加入家庭账本。"
        if any(item.get("error") == "bookkeeping_access_denied" for item in tool_results):
            return "账本权限或会话已变化，请从自动记账重新进入。"
        if any(not item.get("ok") for item in tool_results):
            return "账本服务暂不可用，本次查询未完成。"
        if all(not item.get("userFound", False) for item in tool_results):
            return "账本服务未返回当前账号的资料。"
        if all(int(item.get("orderCount") or 0) == 0 for item in tool_results):
            summaries = [
                str(item.get("summary") or "当前查询范围")
                for item in tool_results
                if item.get("userFound", False)
            ]
            scope = "、".join(summaries) or "当前查询范围"
            return f"{scope}没有查到账单记录。"

        analyses = data.get("analysisResults") or []
        if analyses and not plan.analyses and all(item.get("evidence", {}).get("sufficientData") is False for item in analyses):
            input_names = {"budgetAmount": "预算金额", "startingBalance": "当前存款余额", "targetAmount": "目标金额"}
            missing = list(dict.fromkeys(input_names.get(field, field) for item in analyses
                                        for field in item["evidence"].get("missingInputs", [])))
            if missing:
                return "账单已查询，但完成这次测算还需要确认：" + "、".join(missing) + "。请补充这些条件。"
            reasons = list(dict.fromkeys(item["evidence"].get("reason") or "可用的完整月份不足以支持可靠的推算。" for item in analyses))
            return "当前证据不足以完成这次分析。" + "；".join(reasons)

        return None

    def _assert_no_raw_orders_for_answer(self, orders_json: str) -> None:
        data = self._safe_json_loads(orders_json)
        if self._contains_raw_orders(data):
            raise RuntimeError("内部错误：最终回答 payload 中包含原始账单明细。")

    def _contains_raw_orders(self, value: Any) -> bool:
        if isinstance(value, dict):
            if "ordersInfo" in value or "result" in value:
                return True
            return any(self._contains_raw_orders(item) for item in value.values())
        if isinstance(value, list):
            return any(self._contains_raw_orders(item) for item in value)
        return False

    def _normalize_mode(self, value: str | None) -> str:
        mode = str(value or "").strip()
        if mode in {"家庭版", "家庭", "family", "family_mode"}:
            return "家庭版"
        return "个人版"

    @staticmethod
    def _mask_trace_inputs(inputs: dict[str, Any]) -> dict[str, Any]:
        cleaned = {key: value for key, value in inputs.items() if key != "self"}
        if "family_id" in cleaned and cleaned["family_id"]:
            cleaned["family_id"] = "***"
        return cleaned

    def _safe_json_loads(self, value: str) -> dict:
        # 工具层当前通过 JSON 字符串传递结果，这里统一兜底，避免单个工具异常把整个链路炸掉。
        try:
            data = json.loads(value)
        except json.JSONDecodeError:
            return {"ok": False, "error": "invalid_tool_json", "raw": value}
        return data if isinstance(data, dict) else {"ok": False, "raw": data}

    def _format_history(self, messages: list[dict]) -> str:
        if not messages:
            return "无历史消息。"

        lines = []
        for item in messages:
            if item.get('role') not in {'user', 'assistant'}:
                continue
            content = str(item.get("content", "")).strip()
            if content:
                role = '用户' if item['role'] == 'user' else '助手'
                lines.append(f"{role}: {self._sanitize_history_content(content)}")

        return "\n".join(lines) or "无历史消息。"

    def _sanitize_history_content(self, content: str) -> str:
        markers = [
            '"toolResults"',
            "toolResults",
            '"ordersInfo"',
            "ordersInfo",
            '"calculationToolResults"',
            "calculationToolResults",
            '"comparisonToolResult"',
            "comparisonToolResult",
        ]
        cleaned = content
        for marker in markers:
            marker_index = cleaned.find(marker)
            if marker_index != -1:
                cleaned = cleaned[:marker_index].strip()
        return cleaned[:600]


class ThinkTagStreamParser:
    start_tag = "<think>"
    end_tag = "</think>"

    def __init__(self) -> None:
        self.buffer = ""
        self.in_think = False

    def feed(self, content: str) -> list[dict[str, str]]:
        self.buffer += content
        events: list[dict[str, str]] = []

        while self.buffer:
            if not self.in_think:
                start_index = self.buffer.find(self.start_tag)
                stray_end_index = self.buffer.find(self.end_tag)
                if stray_end_index != -1 and (
                    start_index == -1 or stray_end_index < start_index
                ):
                    self.buffer = self.buffer[stray_end_index + len(self.end_tag) :]
                    continue

            tag = self.end_tag if self.in_think else self.start_tag
            tag_index = self.buffer.find(tag)

            if tag_index == -1:
                keep_length = self._partial_tag_length(self.buffer, tag)
                flush_length = len(self.buffer) - keep_length
                if flush_length <= 0:
                    break

                text = self.buffer[:flush_length]
                self.buffer = self.buffer[flush_length:]
                events.append(self._event(text))
                break

            text = self.buffer[:tag_index]
            if text:
                events.append(self._event(text))

            self.buffer = self.buffer[tag_index + len(tag) :]
            self.in_think = not self.in_think

        return [event for event in events if event["content"]]

    def flush(self) -> list[dict[str, str]]:
        if not self.buffer:
            return []

        event = self._event(self.buffer)
        self.buffer = ""
        return [event] if event["content"] else []

    def _event(self, content: str) -> dict[str, str]:
        return {
            "type": "thinking" if self.in_think else "delta",
            "content": content,
        }

    def _partial_tag_length(self, text: str, tag: str) -> int:
        max_length = min(len(text), len(tag) - 1)
        for length in range(max_length, 0, -1):
            if text[-length:] == tag[:length]:
                return length
        return 0
