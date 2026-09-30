from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

MaterialType = Literal["photo_caption", "measurement", "opinion", "file_ref", "note"]
ReceiptDecision = Literal["accepted", "rejected"]


class MaterialInput(BaseModel):
    item_key: str = Field(min_length=1, max_length=64, description="材料在行动包内的稳定标识")
    title: str = Field(min_length=1, max_length=200)
    material_type: MaterialType = "note"
    content: dict[str, Any] = Field(default_factory=dict)
    sensitive: bool = False
    sensitive_fields: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("item_key")
    @classmethod
    def clean_item_key(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("材料标识不能为空")
        return cleaned

    @field_validator("sensitive_fields")
    @classmethod
    def clean_sensitive_fields(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value if item.strip()]
        if any(any(part == "" for part in field.split(".")) for field in cleaned):
            raise ValueError("敏感字段路径不能为空段")
        return cleaned


class PackageCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=4000)
    incident_ref: str = Field(default="", max_length=100, description="关联的滩涂异常编号")
    materials: list[MaterialInput] = Field(min_length=1, max_length=50)

    @field_validator("materials")
    @classmethod
    def unique_item_keys(cls, value: list[MaterialInput]) -> list[MaterialInput]:
        keys = [item.item_key for item in value]
        if len(set(keys)) != len(keys):
            raise ValueError("材料标识不能重复")
        return value


class PackageUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=4000)
    incident_ref: str | None = Field(default=None, max_length=100)


class MaterialAdd(MaterialInput):
    pass


class MaterialUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    content: dict[str, Any] | None = None
    sensitive: bool | None = None
    sensitive_fields: list[str] | None = Field(default=None, max_length=20)


class DispatchRequest(BaseModel):
    to_department_id: int
    deadline_hours: int = Field(default=48, ge=1, le=720)


class ReceiptItem(BaseModel):
    item_key: str = Field(min_length=1, max_length=64)
    decision: ReceiptDecision
    comment: str = Field(default="", max_length=1000)


class ReceiptRequest(BaseModel):
    decisions: list[ReceiptItem] = Field(min_length=1, max_length=50)

    @field_validator("decisions")
    @classmethod
    def unique_item_keys(cls, value: list[ReceiptItem]) -> list[ReceiptItem]:
        keys = [item.item_key.strip() for item in value]
        if len(set(keys)) != len(keys):
            raise ValueError("同一材料在一次提交中不能出现多次")
        return value


class ReturnRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=2000)


class ReassignRequest(BaseModel):
    to_department_id: int
    reason: str = Field(min_length=1, max_length=2000)
    deadline_hours: int = Field(default=48, ge=1, le=720)


class CorrectionRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=2000)
    materials: list[MaterialInput] = Field(default_factory=list, max_length=50)

    @field_validator("materials")
    @classmethod
    def unique_item_keys(cls, value: list[MaterialInput]) -> list[MaterialInput]:
        keys = [item.item_key for item in value]
        if len(set(keys)) != len(keys):
            raise ValueError("材料标识不能重复")
        return value
