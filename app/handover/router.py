from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.api.dependencies import current_principal
from app.core.pagination import Page
from app.core.security import Principal
from app.database import get_connection, transaction
from app.handover.schemas import (
    CorrectionRequest,
    DispatchRequest,
    MaterialAdd,
    MaterialUpdate,
    PackageCreate,
    PackageUpdate,
    ReassignRequest,
    ReceiptRequest,
    ReturnRequest,
)
from app.handover.service import HandoverService

router = APIRouter(prefix="/api/handover/packages", tags=["跨部门行动包"])


@router.post("", status_code=201)
def create_package(data: PackageCreate, principal: Principal = Depends(current_principal)) -> dict:
    with transaction(immediate=True) as connection:
        return HandoverService(connection).create_package(principal, data.model_dump())


@router.get("")
def list_packages(
    status: str | None = Query(default=None),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    principal: Principal = Depends(current_principal),
) -> dict:
    pagination = Page(page, size)
    total, rows = HandoverService(get_connection()).list_packages(
        principal, status=status, limit=size, offset=pagination.offset
    )
    return {"total": total, "page": page, "size": size, "pages": (total + size - 1) // size, "data": rows}


@router.get("/{package_id}")
def get_package(package_id: int, principal: Principal = Depends(current_principal)) -> dict:
    return HandoverService(get_connection()).detail(principal, package_id)


@router.patch("/{package_id}")
def update_package(package_id: int, data: PackageUpdate, principal: Principal = Depends(current_principal)) -> dict:
    with transaction(immediate=True) as connection:
        return HandoverService(connection).update_package(principal, package_id, data.model_dump(exclude_unset=True))


@router.post("/{package_id}/materials", status_code=201)
def add_material(package_id: int, data: MaterialAdd, principal: Principal = Depends(current_principal)) -> dict:
    with transaction(immediate=True) as connection:
        return HandoverService(connection).add_material(principal, package_id, data.model_dump())


@router.patch("/{package_id}/materials/{item_key}")
def update_material(package_id: int, item_key: str, data: MaterialUpdate, principal: Principal = Depends(current_principal)) -> dict:
    with transaction(immediate=True) as connection:
        return HandoverService(connection).update_material(
            principal, package_id, item_key, data.model_dump(exclude_unset=True)
        )


@router.delete("/{package_id}/materials/{item_key}", status_code=204)
def delete_material(package_id: int, item_key: str, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        HandoverService(connection).delete_material(principal, package_id, item_key)


@router.post("/{package_id}/dispatch")
def dispatch(package_id: int, data: DispatchRequest, principal: Principal = Depends(current_principal)) -> dict:
    with transaction(immediate=True) as connection:
        return HandoverService(connection).dispatch(
            principal, package_id, data.to_department_id, data.deadline_hours
        )


@router.post("/{package_id}/receipts")
def receive(package_id: int, data: ReceiptRequest, principal: Principal = Depends(current_principal)) -> dict:
    with transaction(immediate=True) as connection:
        return HandoverService(connection).receive(
            principal, package_id, [item.model_dump() for item in data.decisions]
        )


@router.post("/{package_id}/return")
def return_package(package_id: int, data: ReturnRequest, principal: Principal = Depends(current_principal)) -> dict:
    with transaction(immediate=True) as connection:
        return HandoverService(connection).return_package(principal, package_id, data.reason)


@router.post("/{package_id}/reassign")
def reassign(package_id: int, data: ReassignRequest, principal: Principal = Depends(current_principal)) -> dict:
    with transaction(immediate=True) as connection:
        return HandoverService(connection).reassign(
            principal, package_id, data.to_department_id, data.reason, data.deadline_hours
        )


@router.post("/{package_id}/corrections")
def correct(package_id: int, data: CorrectionRequest, principal: Principal = Depends(current_principal)) -> dict:
    with transaction(immediate=True) as connection:
        return HandoverService(connection).correct(
            principal, package_id, data.reason, [item.model_dump() for item in data.materials]
        )


@router.get("/{package_id}/versions")
def list_versions(package_id: int, principal: Principal = Depends(current_principal)) -> dict:
    return HandoverService(get_connection()).list_versions(principal, package_id)


@router.get("/{package_id}/versions/{version_no}")
def get_version(package_id: int, version_no: int, principal: Principal = Depends(current_principal)) -> dict:
    return HandoverService(get_connection()).get_version(principal, package_id, version_no)


@router.get("/{package_id}/timeline")
def timeline(package_id: int, principal: Principal = Depends(current_principal)) -> dict:
    return HandoverService(get_connection()).timeline(principal, package_id)
