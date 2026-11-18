"""Pure analysis over authorized query snapshots; reusable after investigation."""

from app.schemas.plan import AnalysisRequest
from app.tools.financial_analysis import _date, merge_orders, run_analysis
from app.tools.source_analysis import observed_source_key


def collect_analysis_results(plan, datasets, today):
    analysis_results = []
    for scope, queries in datasets.items():
        tasks = list(plan.analyses)
        if not any(task.tool == 'sources' for task in tasks):
            tasks.append(AnalysisRequest(tool='sources'))
        for task in tasks:
            bound = [(call, rows) for call, rows in queries
                     if not task.queryIds or set(task.queryIds) & set(call.sourceIds or [call.id])]
            if not bound:
                continue
            filters = {(call.args.cost_type, call.args.remark) for call, _ in bound}
            if len(filters) > 1 and not task.queryIds:
                analysis_results.append({"mode": scope, "tool": task.tool, "evidence": {
                    "sufficientData": False, "reason": "该分析尚未明确关联哪个分类或备注范围，需要确认查询范围。"}})
                continue
            records = merge_orders([rows for _, rows in bound])
            window = plan.analysisWindow
            selected = records
            if window and task.tool != 'budget':
                selected = [order for order in records if _date(order) and window.startDate <= _date(order) <= window.endDate]
            known_keys = {observed_source_key(row) for row in selected if float(row['money']) != 0}
            groups = [{'label': group.label, 'sourceKeys': [key for key in group.sourceKeys if key in known_keys]}
                      for group in plan.sourceGroups if any(key in known_keys for key in group.sourceKeys)]
            result = run_analysis(
                task.tool, selected, as_of=today, metric=task.metric,
                catalog_metric=plan.output.metric,
                source_groups=groups,
                target_month=task.targetMonth,
                changes=[change.model_dump() for change in task.changes],
                budget_amount=task.budgetAmount, starting_balance=task.startingBalance,
                target_amount=task.targetAmount, months=task.months,
                monthly_improvement=task.monthlyImprovement,
                purchase_amount=task.purchaseAmount, reserve_amount=task.reserveAmount,
                protected_categories=task.protectedCategories,
                window_start=window.startDate if window else None,
                window_end=window.endDate if window else None,
            )
            analysis_results.append({"mode": scope, "tool": task.tool, "queryIds": task.queryIds,
                                     "filters": [{"category": category, "remark": remark} for category, remark in sorted(filters)],
                                     "evidence": result})

    return analysis_results
