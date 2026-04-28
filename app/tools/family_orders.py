from langchain_core.tools import StructuredTool
from langsmith import traceable
from pydantic import BaseModel, Field

from app.tools.order_search import mask_trace_inputs, search_orders


class SearchFamilyOrdersInput(BaseModel):
    # family_id 可以由 App/WebView 直接传入；为空时工具会按手机号查询用户资料里的 familyId。
    phone_num: str = Field(description="当前 App 用户手机号，用于验证和兜底获取 familyId")
    family_id: str = Field(default="", description="家庭账本 familyId")
    year: int | None = Field(default=None, description="查询年份，例如 2026")
    month: int | None = Field(default=None, description="查询月份，1-12")
    day: int | None = Field(default=None, description="查询日期，1-31；不传则查询整月")
    cost_type: str = Field(default="不限", description="账单分类，不限表示不过滤")
    remark: str = Field(default="", description="备注关键词，不传则不过滤")


@traceable(
    name="search_family_orders",
    run_type="tool",
    process_inputs=lambda inputs: mask_trace_inputs(inputs),
)
async def search_family_orders(
    phone_num: str,
    family_id: str = "",
    year: int | None = None,
    month: int | None = None,
    day: int | None = None,
    cost_type: str = "不限",
    remark: str = "",
) -> str:
    """查询当前 App 用户所在家庭的家庭版账单。"""

    return await search_orders(
        phone_num=phone_num,
        mode="家庭版",
        family_id=family_id,
        year=year,
        month=month,
        day=day,
        cost_type=cost_type,
        remark=remark,
    )


search_family_orders_tool = StructuredTool.from_function(
    coroutine=search_family_orders,
    name="search_family_orders",
    description=(
        "查询当前 App 用户所在家庭的家庭版账单，可按年、月、日、分类、备注关键词过滤。"
        "family_id 为空时会按手机号查询用户资料获取。"
    ),
    args_schema=SearchFamilyOrdersInput,
)
