from typing import Any

from pydantic import BaseModel, Field


class SearchOrdersRequest(BaseModel):
    mode: str = Field(default="个人版")
    # alias的意思Json的字段名
    family_id: str = Field(default="", alias="familyId")
    phone_num: str = Field(alias="phoneNum")
    user_id: str | None = Field(default=None, alias="userId")
    year: int
    month: int
    day: int
    search_order_remark: str = Field(default="", alias="searchOrderRemark")
    search_cost_type: str = Field(default="不限", alias="searchCostType")
    if_ignore_year: bool = Field(default=False, alias="ifIgnoreYear")
    if_ignore_month: bool = Field(default=False, alias="ifIgnoreMonth")
    if_ignore_day: bool = Field(default=True, alias="ifIgnoreDay")


class OrderInfo(BaseModel):
    id: int | None = None
    year: int
    month: int
    day: int
    money: float
    clock: str = ""
    bankName: str = ""
    orderRemark: str = ""
    costType: str = ""
    userId: str = ""

# 这是response的data格式
class SearchOrdersData(BaseModel):
    # default_factory的意思是如果没有传入这个字段，就用这个默认值,这里会创建一个新的空列表,避免多个实例共享同一个列表
    userInfo: list[dict[str, Any]] = Field(default_factory=list)
    ordersInfo: list[OrderInfo] = Field(default_factory=list)


class ApiResponse(BaseModel):
    success: bool
    message: str
    data: Any = None


class SearchOrdersResponse(ApiResponse):
    data: SearchOrdersData
