from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator


# 材料类型：照片说明 / 检测数值 / 处置意见 等
MaterialKind = Literal["photo_caption", "measurement", "disposal_opinion", "document", "other"]


class MaterialInput(BaseModel):
    key: str = Field(..., min_length=1, max_length=100, description="发起方定义的材料唯一键")
    kind: MaterialKind
    title: str = Field(..., min_length=1, max_length=200)
    content: str = Field("", max_length=20000)
    sensitive: bool = False
    sensitive_fields: list[str] = Field(default_factory=list, max_length=50)


class PackageCreateRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    subject: str = Field(..., min_length=1, max_length=200, description="滩涂异常等事项描述")
    receiver_department_id: int
    deadline_hours: int = Field(..., gt=0, le=24 * 30)
    materials: list[MaterialInput] = Field(..., min_length=1, max_length=200)
    remark: str = Field("", max_length=2000)

    @model_validator(mode="after")
    def unique_material_keys(self) -> "PackageCreateRequest":
        keys = [item.key for item in self.materials]
        if len(set(keys)) != len(keys):
            raise ValueError("材料 key 不能重复")
        return self


class PackageRevisionRequest(BaseModel):
    """退回修订后发起方重新提交，生成不可覆盖的新版本。"""

    materials: list[MaterialInput] = Field(..., min_length=1, max_length=200)
    deadline_hours: int = Field(..., gt=0, le=24 * 30)
    remark: str = Field("", max_length=2000)

    @model_validator(mode="after")
    def unique_material_keys(self) -> "PackageRevisionRequest":
        keys = [item.key for item in self.materials]
        if len(set(keys)) != len(keys):
            raise ValueError("材料 key 不能重复")
        return self


class ItemAckEntry(BaseModel):
    material_key: str = Field(..., min_length=1, max_length=100)
    accepted: bool
    note: str = Field("", max_length=1000)


class ItemAckRequest(BaseModel):
    """接收方逐项确认或退回；支持部分接收。"""

    entries: list[ItemAckEntry] = Field(..., min_length=1, max_length=200)

    @model_validator(mode="after")
    def unique_entries(self) -> "ItemAckRequest":
        keys = [item.material_key for item in self.entries]
        if len(set(keys)) != len(keys):
            raise ValueError("同一材料不能重复出现在一次提交中")
        return self


class ReturnRequest(BaseModel):
    """接收方整体退回修订。"""

    reason: str = Field(..., min_length=1, max_length=2000)


class ForwardRequest(BaseModel):
    """接收方确认完整后转交给下一部门。"""

    next_department_id: int
    deadline_hours: int = Field(..., gt=0, le=24 * 30)
    note: str = Field("", max_length=2000)


class ReassignRequest(BaseModel):
    """值班人员对超时交接重新分派。"""

    new_receiver_department_id: int
    deadline_hours: int = Field(..., gt=0, le=24 * 30)
    reason: str = Field(..., min_length=1, max_length=2000)


class CompleteRequest(BaseModel):
    conclusion: str = Field(..., min_length=1, max_length=4000)
