"""Bounded grouping and period comparisons over already-authorized ledger records."""

from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from statistics import median
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, model_validator

from app.tools.source_analysis import money, observed_source_key, source_aliases, source_rows


Dimension = Literal["month", "year", "category", "member", "source", "bank", "weekday"]
Label = Annotated[str, Field(min_length=1, max_length=160)]
Selectors = Annotated[list[Label], Field(max_length=60)]
WEEKDAYS = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")


class AggregatePeriod(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    label: Label
    startDate: date
    endDate: date

    @model_validator(mode="after")
    def ordered(self):
        if self.endDate < self.startDate:
            raise ValueError("汇总区间的结束日期早于开始日期")
        return self


class AggregateFilters(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    includeCategories: Selectors = Field(default_factory=list)
    excludeCategories: Selectors = Field(default_factory=list)
    includeMembers: Selectors = Field(default_factory=list)
    excludeMembers: Selectors = Field(default_factory=list)
    includeSources: Selectors = Field(default_factory=list)
    excludeSources: Selectors = Field(default_factory=list)
    remarkContains: str = Field(default="", max_length=160)
    excludeRemarks: Selectors = Field(default_factory=list)
    minAmount: FiniteFloat | None = Field(default=None, ge=0)
    maxAmount: FiniteFloat | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def ordered(self):
        if self.minAmount is not None and self.maxAmount is not None and self.minAmount > self.maxAmount:
            raise ValueError("汇总最低金额超过最高金额")
        return self


class AggregateSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    groupBy: list[Dimension] = Field(default_factory=list, max_length=4)
    periods: list[AggregatePeriod] = Field(default_factory=list, max_length=8)
    filters: AggregateFilters = Field(default_factory=AggregateFilters)
    limit: int = Field(default=50, ge=1, le=200)

    @model_validator(mode="after")
    def unique(self):
        if len(set(self.groupBy)) != len(self.groupBy):
            raise ValueError("汇总维度不能重复")
        if len({p.label for p in self.periods}) != len(self.periods):
            raise ValueError("比较区间名称不能重复")
        return self


@dataclass
class Bucket:
    income: int = 0
    expense: int = 0
    count: int = 0
    income_count: int = 0
    expense_count: int = 0
    samples: list[int] = field(default_factory=list)

    def add(self, cents, *, sample=False):
        self.count += 1
        if cents > 0:
            self.income += cents
            self.income_count += 1
        elif cents < 0:
            self.expense -= cents
            self.expense_count += 1
        if sample:
            self.samples.append(abs(cents))

    def amount(self, metric):
        return {"income": self.income, "expense": self.expense, "net": self.income - self.expense}[metric]

    def summary(self):
        return {"income": self.income / 100, "expense": self.expense / 100,
                "net": (self.income - self.expense) / 100, "count": self.count,
                "incomeCount": self.income_count, "expenseCount": self.expense_count}

    def statistics(self, metric, period):
        directional = metric in {"income", "expense"}
        amount = self.amount(metric) if metric != "all" else None
        return {**self.summary(), "amount": amount / 100 if amount is not None else None,
                "meanTransactionAmount": money(Decimal(amount) / (self.count * 100)) if self.samples else None,
                "medianTransactionAmount": money(Decimal(str(median(self.samples))) / 100) if self.samples else None,
                "sharePercent": money(Decimal(amount) * 100 / period.amount(metric))
                if directional and period.amount(metric) else None}


def _order_date(row):
    return date(int(row["year"]), int(row["month"]), int(row["day"]))


def _query_union(intervals):
    if intervals is None:
        return None
    parsed = []
    for interval in intervals:
        start = date.fromisoformat(str(interval["startDate"]))
        end = date.fromisoformat(str(interval["endDate"]))
        if end < start:
            raise ValueError("实际查询区间结束早于开始")
        parsed.append((start, end))
    merged = []
    for start, end in sorted(parsed):
        if merged and start.toordinal() <= merged[-1][1].toordinal() + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _intervals(spec, orders, as_of, window_start, window_end, query_union):
    if spec.periods:
        return [(p.label, p.startDate, p.endDate) for p in spec.periods]
    if query_union:
        return [("查询区间", window_start or query_union[0][0], window_end or query_union[-1][1])]
    end = min(window_end or as_of, as_of)
    start = window_start or min((_order_date(row) for row in orders if _order_date(row) <= end), default=end)
    return [("查询区间", start, window_end or as_of)]


def _period_state(label, start, end, window_start, window_end, as_of, query_union):
    lower, upper = max(start, window_start or start), min(end, window_end or end, as_of)
    available = [(lower, upper)] if query_union is None else query_union
    ranges = [(max(lower, a), min(upper, b)) for a, b in available if max(lower, a) <= min(upper, b)]
    coverage = "complete" if ranges == [(start, end)] else ("partial" if ranges else "not_queried")
    return {"period": label, "requestedStart": start, "requestedEnd": end,
            "start": lower, "end": upper, "ranges": ranges, "queryCoverage": coverage,
            "recordCount": 0, "first": None, "last": None, "groups": defaultdict(Bucket), "included": Bucket()}


def _source_index(orders, spec, source_groups):
    aliases = source_aliases(orders, source_groups)
    if "source" not in spec.groupBy and not spec.filters.includeSources and not spec.filters.excludeSources:
        return {}
    original = source_rows(orders, {})
    grouped = source_rows(orders, aliases) if aliases else original
    labels = {r["sourceKey"]: r["source"] for r in original}
    return {key: (row["source"], {row["source"], row["sourceKey"], key, labels[key]})
            for row in grouped for key in row["observedSourceKeys"]}


def _allowed(values, include, exclude):
    return (not include or not values.isdisjoint(include)) and values.isdisjoint(exclude)


def _accepts(filters, category, member, sources, remark, amount):
    return (_allowed({category}, filters.includeCategories, filters.excludeCategories)
            and _allowed(member, filters.includeMembers, filters.excludeMembers)
            and _allowed(sources, filters.includeSources, filters.excludeSources)
            and filters.remarkContains in remark and not any(term in remark for term in filters.excludeRemarks)
            and (filters.minAmount is None or amount >= Decimal(str(filters.minAmount)))
            and (filters.maxAmount is None or amount <= Decimal(str(filters.maxAmount))))


def _dimension_fields(dimensions, group):
    fields = dict(zip(dimensions, group))
    if "member" in fields:
        fields["memberKey"], fields["member"] = fields["member"]
    return fields


def _comparison_groups(groups, keep):
    combined = defaultdict(Bucket)
    for key, bucket in groups.items():
        target = combined[tuple(key[i] for i in keep)]
        target.income += bucket.income
        target.expense += bucket.expense
    return combined


def _comparisons(states, dimensions, comparison_dimensions, metric):
    if len(states) < 2:
        return []
    keep = [dimensions.index(name) for name in comparison_dimensions]
    periods = [(state, _comparison_groups(state["groups"], keep) if dimensions != comparison_dimensions else state["groups"])
               for state in states]
    (first, first_groups), *others = periods
    results = []
    for other, other_groups in others:
        keys = first_groups.keys() | other_groups.keys()
        if not comparison_dimensions:
            keys = {()}
        for key in sorted(keys):
            for measure in (("income", "expense", "net") if metric == "all" else (metric,)):
                values = []
                for state, groups in ((first, first_groups), (other, other_groups)):
                    group = groups.get(key)
                    known = state["recordCount"] and state["queryCoverage"] == "complete"
                    values.append((group.amount(measure) if group else 0) if known else None)
                before, after = values
                delta = after - before if before is not None and after is not None else None
                results.append({**_dimension_fields(comparison_dimensions, key), "metric": measure,
                                "beforePeriod": first["period"], "afterPeriod": other["period"],
                                "before": before / 100 if before is not None else None,
                                "after": after / 100 if after is not None else None,
                                "change": delta / 100 if delta is not None else None,
                                "changePercent": money(Decimal(delta) * 100 / abs(before)) if delta is not None and before else None})
    return results


def aggregate_analysis(orders, *, as_of, metric="all", aggregate=None, source_groups=None,
                       window_start=None, window_end=None, queried_intervals=None, **kwargs):
    if metric not in {"income", "expense", "net", "all"}:
        raise ValueError("未知的汇总指标")
    spec = AggregateSpec.model_validate(aggregate if aggregate is not None else {})
    query_union = _query_union(queried_intervals)
    query_starts = [start for start, _ in query_union or []]
    intervals = _intervals(spec, orders, as_of, window_start, window_end, query_union)
    states = [_period_state(label, start, end, window_start, window_end, as_of, query_union)
              for label, start, end in intervals]
    audit = {name: Bucket() for name in ("scoped", "excludedByMetric", "excludedByFilters", "included")}
    sources = _source_index(orders, spec, source_groups)
    directional = metric in {"income", "expense"}
    for order in orders:
        when = _order_date(order)
        if query_union is not None:
            i = bisect_right(query_starts, when) - 1
            if i < 0 or when > query_union[i][1]:
                continue
        periods = [state for state in states if state["start"] <= when <= state["end"]]
        if not periods:
            continue
        cents = int((Decimal(str(order["money"])) * 100).to_integral_value(rounding=ROUND_HALF_UP))
        audit["scoped"].add(cents)
        for state in periods:
            state["recordCount"] += 1
            state["first"] = min(when, state["first"] or when)
            state["last"] = max(when, state["last"] or when)
        if (metric == "income" and cents <= 0) or (metric == "expense" and cents >= 0):
            audit["excludedByMetric"].add(cents)
            continue
        category = str(order.get("costType") or "").strip() or "未分类"
        member_key = order.get("memberKey") or "member_unknown"
        member = order.get("memberName") or "未知成员"
        source, selectors = sources.get(observed_source_key(order), ("未标明来源", set())) if sources else ("未标明来源", set())
        if not _accepts(spec.filters, category, {member_key, member}, selectors,
                        str(order.get("orderRemark") or ""), Decimal(abs(cents)) / 100):
            audit["excludedByFilters"].add(cents)
            continue
        audit["included"].add(cents)
        fields = {"month": when.strftime("%Y-%m"), "year": when.year, "weekday": WEEKDAYS[when.weekday()],
                  "category": category, "member": (member_key, member), "source": source,
                  "bank": str(order.get("bankName") or "").strip() or "未注明"}
        key = tuple(fields[name] for name in spec.groupBy)
        for state in periods:
            state["included"].add(cents)
            state["groups"][key].add(cents, sample=directional)

    rows, coverage = [], []
    for state in states:
        ranges = state["ranges"]
        coverage.append({"period": state["period"], "startDate": state["requestedStart"].isoformat(),
                         "endDate": state["requestedEnd"].isoformat(),
                         "effectiveStartDate": ranges[0][0].isoformat() if ranges else None,
                         "effectiveEndDate": ranges[-1][1].isoformat() if ranges else None,
                         "queryCoverage": state["queryCoverage"],
                         "queriedRanges": [{"startDate": a.isoformat(), "endDate": b.isoformat()} for a, b in ranges],
                         "totals": state["included"].summary(),
                         "observed": bool(state["recordCount"]), "recordCount": state["recordCount"],
                         "matchedCount": state["included"].count,
                         "firstRecordedDate": state["first"].isoformat() if state["first"] else None,
                         "lastRecordedDate": state["last"].isoformat() if state["last"] else None})
        if not spec.groupBy and state["recordCount"]:
            state["groups"].setdefault((), Bucket())
        for key, bucket in sorted(state["groups"].items()):
            rows.append({**({"period": state["period"]} if spec.periods else {}),
                         **_dimension_fields(spec.groupBy, key), **bucket.statistics(metric, state["included"])})
    rows.sort(key=lambda r: abs(r["amount"]) if r["amount"] is not None else r["income"] + r["expense"], reverse=True)
    comparison_dimensions = [name for name in spec.groupBy if name not in {"month", "year"}]
    comparisons = _comparisons(states, spec.groupBy, comparison_dimensions, metric)
    comparisons.sort(key=lambda r: abs(r["change"]) if r["change"] is not None else -1, reverse=True)
    return {"metric": metric, "dimensions": (["period"] if spec.periods else []) + spec.groupBy,
            "rows": rows[:spec.limit], "totalGroupCount": len(rows), "omittedGroupCount": max(0, len(rows) - spec.limit),
            "totals": audit["included"].summary(), "filterAudit": {name: bucket.summary() for name, bucket in audit.items()},
            "appliedFilters": spec.filters.model_dump(exclude_defaults=True), "coverage": coverage,
            "totalsBasis": "requested_periods" if all(s["queryCoverage"] == "complete" for s in states) else "queried_subset",
            "comparisonDimensions": comparison_dimensions, "comparisons": comparisons[:spec.limit], "totalComparisonCount": len(comparisons),
            "omittedComparisonCount": max(0, len(comparisons) - spec.limit),
            "definitions": ["金额四舍五入到分。收入和支出为正数，净额为收入减支出；未筛选时保留转入等所有账本记录。",
                            "count 是选中账单笔数；定向统计的均值和中位数按每笔绝对金额计算。占比以同区间该方向的筛选后合计为分母。",
                            "显式区间分别汇总；总计和筛选审计按区间并集统计一次。行按金额绝对值（双向时按收支合计）排序。",
                            "跨区间比较先合并每期内的月份、年份，再按其他所选维度对齐；普通分组行保留全部所选维度。",
                            "变化额为后期减首期，变化率以首期绝对值为分母，分母为零时留空。"],
            "limitations": ["未完整查询的区间仅展示实际查询范围内的已记金额，无法用于计算完整跨期变化。",
                            "区间内完全缺账代表覆盖未知；有其他账单时，某组未出现仅表示该组没有记录。",
                            "同名来源按账本标签汇总，其实际归属需结合备注确认。账单笔数和每笔均额无法区分商品单价、数量和购买次数。"]}
