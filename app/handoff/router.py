from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.api.dependencies import current_principal
from app.core.pagination import Page
from app.core.security import Principal
from app.database import get_connection, transaction
from app.handoff.schemas import (
    CompleteRequest,
    ForwardRequest,
    ItemAckRequest,
    PackageCreateRequest,
    PackageRevisionRequest,
    ReassignRequest,
    ReturnRequest,
)
from app.handoff.service import HandoffService

router = APIRouter(prefix="/api/handoff/packages", tags=["跨部门行动包"])


def _ensure_mutable(package_id: int, principal: Principal, *, permission: str = "handoff.write") -> None:
    # 在自动提交连接上预检：封存冲突要独立留痕，不能随业务事务回滚
    HandoffService(get_connection()).ensure_mutable(package_id, principal, permission=permission)


@router.post("", status_code=201)
def create_package(data: PackageCreateRequest, principal: Principal = Depends(current_principal)) -> dict:
    with transaction(immediate=True) as connection:
        return HandoffService(connection).create_package(principal, data.model_dump())


@router.get("")
def list_packages(
    status: str | None = None,
    department_id: int | None = None,
    overdue_only: bool = False,
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    principal: Principal = Depends(current_principal),
) -> dict:
    pagination = Page(page, size)
    result = HandoffService(get_connection()).list_packages(
        principal,
        status=status,
        department_id=department_id,
        overdue_only=overdue_only,
        limit=size,
        offset=pagination.offset,
    )
    return {
        "total": result["total"],
        "page": pagination.number,
        "size": pagination.size,
        "pages": (result["total"] + pagination.size - 1) // pagination.size,
        "data": result["data"],
    }


@router.get("/overdue")
def list_overdue(principal: Principal = Depends(current_principal)) -> dict:
    return {"data": HandoffService(get_connection()).list_overdue(principal)}


@router.get("/{package_id}")
def get_package(package_id: int, principal: Principal = Depends(current_principal)) -> dict:
    return HandoffService(get_connection()).detail(package_id, principal=principal)


@router.get("/{package_id}/chain")
def get_chain(package_id: int, principal: Principal = Depends(current_principal)) -> dict:
    return {"data": HandoffService(get_connection()).chain(principal, package_id)}


@router.get("/{package_id}/versions/{version_no}")
def get_version(package_id: int, version_no: int, principal: Principal = Depends(current_principal)) -> dict:
    return HandoffService(get_connection()).version_detail(principal, package_id, version_no)


@router.post("/{package_id}/acknowledgments", status_code=201)
def acknowledge_items(
    package_id: int,
    data: ItemAckRequest,
    principal: Principal = Depends(current_principal),
) -> dict:
    _ensure_mutable(package_id, principal)
    with transaction(immediate=True) as connection:
        return HandoffService(connection).ack_items(
            principal, package_id, [entry.model_dump() for entry in data.entries]
        )


@router.post("/{package_id}/return")
def return_package(
    package_id: int,
    data: ReturnRequest,
    principal: Principal = Depends(current_principal),
) -> dict:
    _ensure_mutable(package_id, principal)
    with transaction(immediate=True) as connection:
        return HandoffService(connection).return_package(principal, package_id, data.reason)


@router.post("/{package_id}/revisions", status_code=201)
def revise_package(
    package_id: int,
    data: PackageRevisionRequest,
    principal: Principal = Depends(current_principal),
) -> dict:
    _ensure_mutable(package_id, principal)
    with transaction(immediate=True) as connection:
        return HandoffService(connection).revise(principal, package_id, data.model_dump())


@router.post("/{package_id}/forward")
def forward_package(
    package_id: int,
    data: ForwardRequest,
    principal: Principal = Depends(current_principal),
) -> dict:
    _ensure_mutable(package_id, principal)
    with transaction(immediate=True) as connection:
        return HandoffService(connection).forward(principal, package_id, data.model_dump())


@router.post("/{package_id}/reassign")
def reassign_package(
    package_id: int,
    data: ReassignRequest,
    principal: Principal = Depends(current_principal),
) -> dict:
    _ensure_mutable(package_id, principal, permission="handoff.dispatch")
    with transaction(immediate=True) as connection:
        return HandoffService(connection).reassign(principal, package_id, data.model_dump())


@router.post("/{package_id}/complete")
def complete_package(
    package_id: int,
    data: CompleteRequest,
    principal: Principal = Depends(current_principal),
) -> dict:
    _ensure_mutable(package_id, principal)
    with transaction(immediate=True) as connection:
        return HandoffService(connection).complete(principal, package_id, data.conclusion)
