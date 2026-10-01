from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from datetime import timedelta
from typing import Any

from app.core.clock import Clock, SystemClock, from_storage, to_storage
from app.core.errors import ConflictError, NotFoundError, PermissionDeniedError, ValidationError
from app.core.security import Principal
from app.services.audit import AuditContext, AuditService

PACKAGE_STATUS_IN_PROGRESS = "in_progress"
PACKAGE_STATUS_COMPLETED = "completed"

RECORD_PENDING = "pending"
RECORD_PARTIAL = "partial"
RECORD_ACCEPTED = "accepted"
RECORD_RETURNED = "returned"
RECORD_FORWARDED = "forwarded"
RECORD_REASSIGNED = "reassigned"
RECORD_COMPLETED = "completed"


def manifest_digest(materials: list[dict[str, Any]]) -> str:
    canonical = json.dumps(materials, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class HandoffService:
    """跨部门行动包与交接链服务。

    所有写操作都在外层 IMMEDIATE 事务内执行；材料一经生成版本即不可覆盖，
    完成的行动包会被封存，任何修改尝试都会被拒绝并写入拒绝审计。
    """

    def __init__(self, connection: sqlite3.Connection, clock: Clock | None = None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        self.audit = AuditService(connection, self.clock)

    # ------------------------------------------------------------------ 发起

    def create_package(self, principal: Principal, data: dict[str, Any]) -> dict[str, Any]:
        principal.require("handoff.write")
        receiver_department_id = int(data["receiver_department_id"])
        self._require_active_department(receiver_department_id)
        if principal.department_id is not None and receiver_department_id == principal.department_id:
            raise ValidationError("接收方不能与发起部门相同")
        materials = [self._normalize_material(item) for item in data["materials"]]
        now = self.clock.now()
        now_text = to_storage(now)
        deadline_text = to_storage(now + timedelta(hours=int(data["deadline_hours"])))
        code = "PKG-" + now.strftime("%Y%m%d") + "-" + secrets.token_hex(4)
        cursor = self.connection.execute(
            "INSERT INTO handoff_packages(code,title,subject,initiator_user_id,initiator_name,"
            "initiator_department_id,status,current_version_no,locked,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,1,0,?,?)",
            (
                code,
                data["title"].strip(),
                data["subject"].strip(),
                principal.user_id,
                principal.display_name,
                principal.department_id,
                PACKAGE_STATUS_IN_PROGRESS,
                now_text,
                now_text,
            ),
        )
        package_id = int(cursor.lastrowid)
        self._insert_version(package_id, 1, materials, principal, data.get("remark", ""), now_text)
        self._insert_record(
            package_id,
            seq=1,
            version_no=1,
            sender=principal,
            receiver_department_id=receiver_department_id,
            deadline_text=deadline_text,
            note=data.get("remark", ""),
            now_text=now_text,
        )
        self.audit.record(
            AuditContext(principal.user_id, principal.display_name),
            action="handoff.package.create",
            resource_type="handoff_package",
            resource_id=package_id,
            after={"code": code, "receiver_department_id": receiver_department_id, "version": 1},
        )
        return self.detail(package_id, principal=principal)

    # ------------------------------------------------------------ 逐项确认

    def ack_items(self, principal: Principal, package_id: int, entries: list[dict[str, Any]]) -> dict[str, Any]:
        package = self._require_package(package_id)
        principal.require("handoff.write")
        self._require_unlocked(package, principal)
        record = self._require_active_record(package_id)
        self._require_receiver(principal, record)
        materials = self._version_materials(package_id, record["version_no"])
        material_by_key = {item["key"]: item for item in materials}
        requested_keys = [item["material_key"] for item in entries]
        if len(set(requested_keys)) != len(requested_keys):
            raise ValidationError("一次提交中同一材料只能出现一次")
        unknown = [key for key in requested_keys if key not in material_by_key]
        if unknown:
            raise ValidationError(f"材料不属于当前版本清单：{'、'.join(unknown)}")
        already = {
            row["material_key"]
            for row in self.connection.execute(
                "SELECT material_key FROM handoff_item_acks WHERE handoff_record_id=?",
                (record["id"],),
            ).fetchall()
        }
        duplicated = [key for key in requested_keys if key in already]
        if duplicated:
            # 重复确认：同一条交接记录上已经逐项处理过，不能覆盖既有结论
            raise ConflictError(f"材料已确认，不能重复确认：{'、'.join(duplicated)}")
        now_text = to_storage(self.clock.now())
        accepted_keys: list[str] = []
        rejected_keys: list[str] = []
        for entry in entries:
            accepted = bool(entry["accepted"])
            self.connection.execute(
                "INSERT INTO handoff_item_acks(package_id,handoff_record_id,version_no,material_key,"
                "accepted,note,acked_by_user_id,acked_by_name,acked_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    package_id,
                    record["id"],
                    record["version_no"],
                    entry["material_key"],
                    1 if accepted else 0,
                    entry.get("note", "").strip(),
                    principal.user_id,
                    principal.display_name,
                    now_text,
                ),
            )
            (accepted_keys if accepted else rejected_keys).append(entry["material_key"])
        ack_count = int(self.connection.execute(
            "SELECT COUNT(*) FROM handoff_item_acks WHERE handoff_record_id=?", (record["id"],)
        ).fetchone()[0])
        all_acked = ack_count == len(materials)
        if all_acked and not rejected_keys:
            record_status = RECORD_ACCEPTED
            acknowledged_at = now_text
        elif rejected_keys and all_acked:
            # 全部处理完且存在退回项：整条交接退回修订，必须重新出版本
            record_status = RECORD_RETURNED
            acknowledged_at = now_text
        else:
            # 部分接收：保持待处理，允许继续逐项确认
            record_status = RECORD_PARTIAL
            acknowledged_at = None
        self.connection.execute(
            "UPDATE handoff_records SET status=?,acknowledged_at=COALESCE(?,acknowledged_at),note=? WHERE id=?",
            (record_status, acknowledged_at, record["note"], record["id"]),
        )
        self.audit.record(
            AuditContext(principal.user_id, principal.display_name),
            action="handoff.items.ack",
            resource_type="handoff_package",
            resource_id=package_id,
            after={
                "record_id": record["id"],
                "accepted": accepted_keys,
                "rejected": rejected_keys,
                "record_status": record_status,
                "ack_count": ack_count,
            },
            metadata={"partial": record_status == RECORD_PARTIAL},
        )
        return self.detail(package_id, principal=principal)

    # ------------------------------------------------------------ 整体退回

    def return_package(self, principal: Principal, package_id: int, reason: str) -> dict[str, Any]:
        package = self._require_package(package_id)
        self._require_unlocked(package, principal)
        principal.require("handoff.write")
        record = self._require_active_record(package_id)
        self._require_receiver(principal, record)
        now_text = to_storage(self.clock.now())
        self.connection.execute(
            "UPDATE handoff_records SET status=?,acknowledged_at=?,return_reason=? WHERE id=?",
            (RECORD_RETURNED, now_text, reason.strip(), record["id"]),
        )
        self.audit.record(
            AuditContext(principal.user_id, principal.display_name),
            action="handoff.package.return",
            resource_type="handoff_package",
            resource_id=package_id,
            after={"record_id": record["id"], "reason": reason.strip()},
        )
        return self.detail(package_id, principal=principal)

    # ------------------------------------------------------------ 退回后修订

    def revise(self, principal: Principal, package_id: int, data: dict[str, Any]) -> dict[str, Any]:
        package = self._require_package(package_id)
        self._require_unlocked(package, principal)
        principal.require("handoff.write")
        record = self._require_last_record(package_id)
        self._require_initiator(principal, package)
        if record["status"] != RECORD_RETURNED:
            raise ConflictError("只有被退回的行动包才能重新提交版本")
        materials = [self._normalize_material(item) for item in data["materials"]]
        new_version = int(package["current_version_no"]) + 1
        now = self.clock.now()
        now_text = to_storage(now)
        self._insert_version(
            package_id, new_version, materials, principal, data.get("remark", ""), now_text,
            revised_from_record_id=record["id"],
        )
        self.connection.execute(
            "UPDATE handoff_packages SET current_version_no=?,updated_at=? WHERE id=?",
            (new_version, now_text, package_id),
        )
        self._insert_record(
            package_id,
            seq=int(record["seq"]) + 1,
            version_no=new_version,
            sender=principal,
            receiver_department_id=int(record["receiver_department_id"]),
            deadline_text=to_storage(now + timedelta(hours=int(data["deadline_hours"]))),
            note=data.get("remark", ""),
            now_text=now_text,
        )
        self.audit.record(
            AuditContext(principal.user_id, principal.display_name),
            action="handoff.package.revise",
            resource_type="handoff_package",
            resource_id=package_id,
            before={"version": package["current_version_no"]},
            after={"version": new_version, "manifest_digest": manifest_digest(materials)},
        )
        return self.detail(package_id, principal=principal)

    # ---------------------------------------------------------------- 转交

    def forward(self, principal: Principal, package_id: int, data: dict[str, Any]) -> dict[str, Any]:
        package = self._require_package(package_id)
        self._require_unlocked(package, principal)
        principal.require("handoff.write")
        record = self._require_last_record(package_id)
        self._require_receiver(principal, record)
        if record["status"] != RECORD_ACCEPTED:
            raise ConflictError("只有全部材料逐项确认通过后才能转交")
        next_department_id = int(data["next_department_id"])
        self._require_active_department(next_department_id)
        if next_department_id == int(record["receiver_department_id"]):
            raise ValidationError("不能转交给当前接收部门自身")
        now = self.clock.now()
        now_text = to_storage(now)
        self.connection.execute(
            "UPDATE handoff_records SET status=? WHERE id=?",
            (RECORD_FORWARDED, record["id"]),
        )
        self._insert_record(
            package_id,
            seq=int(record["seq"]) + 1,
            version_no=int(record["version_no"]),
            sender=principal,
            receiver_department_id=next_department_id,
            deadline_text=to_storage(now + timedelta(hours=int(data["deadline_hours"]))),
            note=data.get("note", ""),
            now_text=now_text,
        )
        self.audit.record(
            AuditContext(principal.user_id, principal.display_name),
            action="handoff.package.forward",
            resource_type="handoff_package",
            resource_id=package_id,
            after={"record_id": record["id"], "next_department_id": next_department_id},
        )
        return self.detail(package_id, principal=principal)

    # ------------------------------------------------------------ 超时接管

    def reassign(self, principal: Principal, package_id: int, data: dict[str, Any]) -> dict[str, Any]:
        package = self._require_package(package_id)
        self._require_unlocked(package, principal)
        principal.require("handoff.dispatch")
        record = self._require_active_record(package_id)
        now = self.clock.now()
        if from_storage(record["deadline_at"]) is None or from_storage(record["deadline_at"]) > now:
            raise ConflictError("交接尚未超期，不能重新分派")
        new_department_id = int(data["new_receiver_department_id"])
        self._require_active_department(new_department_id)
        if new_department_id == int(record["receiver_department_id"]):
            raise ValidationError("重新分派的部门不能与原接收部门相同")
        now_text = to_storage(now)
        self.connection.execute(
            "UPDATE handoff_records SET status=?,dispatched_by_user_id=?,dispatched_by_name=? WHERE id=?",
            (RECORD_REASSIGNED, principal.user_id, principal.display_name, record["id"]),
        )
        self._insert_record(
            package_id,
            seq=int(record["seq"]) + 1,
            version_no=int(record["version_no"]),
            sender=principal,
            receiver_department_id=new_department_id,
            deadline_text=to_storage(now + timedelta(hours=int(data["deadline_hours"]))),
            note=data["reason"],
            now_text=now_text,
            dispatched_by=principal,
        )
        self.audit.record(
            AuditContext(principal.user_id, principal.display_name),
            action="handoff.package.reassign",
            resource_type="handoff_package",
            resource_id=package_id,
            after={
                "record_id": record["id"],
                "new_department_id": new_department_id,
                "reason": data["reason"].strip(),
            },
        )
        return self.detail(package_id, principal=principal)

    # ---------------------------------------------------------------- 完成

    def complete(self, principal: Principal, package_id: int, conclusion: str) -> dict[str, Any]:
        package = self._require_package(package_id)
        self._require_unlocked(package, principal)
        principal.require("handoff.write")
        record = self._require_last_record(package_id)
        self._require_receiver(principal, record)
        if record["status"] != RECORD_ACCEPTED:
            raise ConflictError("只有全部材料确认通过后才能封存行动包")
        now_text = to_storage(self.clock.now())
        self.connection.execute(
            "UPDATE handoff_records SET status=?,acknowledged_at=COALESCE(acknowledged_at,?) WHERE id=?",
            (RECORD_COMPLETED, now_text, record["id"]),
        )
        self.connection.execute(
            "UPDATE handoff_packages SET status=?,locked=1,conclusion=?,completed_at=?,updated_at=? WHERE id=?",
            (PACKAGE_STATUS_COMPLETED, conclusion.strip(), now_text, now_text, package_id),
        )
        self.audit.record(
            AuditContext(principal.user_id, principal.display_name),
            action="handoff.package.complete",
            resource_type="handoff_package",
            resource_id=package_id,
            after={"conclusion": conclusion.strip()},
        )
        return self.detail(package_id, principal=principal)

    # ---------------------------------------------------------------- 查询

    def list_packages(
        self,
        principal: Principal,
        *,
        status: str | None = None,
        department_id: int | None = None,
        overdue_only: bool = False,
        limit: int = 20,
        offset: int = 0,
    ) -> dict[str, Any]:
        principal.require("handoff.read")
        conditions: list[str] = []
        params: list[Any] = []
        if status:
            if status not in {PACKAGE_STATUS_IN_PROGRESS, PACKAGE_STATUS_COMPLETED}:
                raise ValidationError("行动包状态不合法")
            conditions.append("p.status=?")
            params.append(status)
        if department_id is not None:
            self._require_department_scope(principal, department_id)
            conditions.append(
                "EXISTS(SELECT 1 FROM handoff_records r WHERE r.package_id=p.id AND r.receiver_department_id=?)"
            )
            params.append(department_id)
        elif not self._has_global_visibility(principal):
            if principal.department_id is not None:
                conditions.append(
                    "(p.initiator_department_id=? OR EXISTS(SELECT 1 FROM handoff_records r "
                    "WHERE r.package_id=p.id AND r.receiver_department_id=?))"
                )
                params.extend([principal.department_id, principal.department_id])
            else:
                conditions.append("p.initiator_user_id=?")
                params.append(principal.user_id)
        if overdue_only:
            conditions.append(
                "EXISTS(SELECT 1 FROM handoff_records r WHERE r.package_id=p.id "
                "AND r.status IN ('pending','partial') AND r.deadline_at<=?)"
            )
            params.append(to_storage(self.clock.now()))
        where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
        total = int(self.connection.execute(
            "SELECT COUNT(*) FROM handoff_packages p" + where, tuple(params)
        ).fetchone()[0])
        rows = self.connection.execute(
            "SELECT p.* FROM handoff_packages p" + where + " ORDER BY p.id DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()
        return {"total": total, "data": [self._package_summary(dict(row)) for row in rows]}

    def detail(self, package_id: int, principal: Principal | None = None) -> dict[str, Any]:
        package_row = self.connection.execute(
            "SELECT * FROM handoff_packages WHERE id=?", (package_id,)
        ).fetchone()
        if package_row is None:
            raise NotFoundError("行动包不存在")
        package = dict(package_row)
        if principal is not None:
            principal.require("handoff.read")
            self._require_can_view(principal, package)
        versions = []
        for row in self.connection.execute(
            "SELECT id,version_no,materials_json,manifest_digest,revised_from_record_id,created_by_name,remark,created_at "
            "FROM handoff_versions WHERE package_id=? ORDER BY version_no",
            (package_id,),
        ).fetchall():
            item = dict(row)
            item["digest_verified"] = manifest_digest(json.loads(item.pop("materials_json"))) == item["manifest_digest"]
            versions.append(item)
        records = [
            self._record_view(dict(row))
            for row in self.connection.execute(
                "SELECT * FROM handoff_records WHERE package_id=? ORDER BY seq",
                (package_id,),
            ).fetchall()
        ]
        package["materials"] = self._version_materials(package_id, package["current_version_no"])
        package["locked"] = bool(package["locked"])
        package["versions"] = versions
        package["handoff_chain"] = records
        package["is_overdue"] = self._is_overdue(package)
        return package

    def chain(self, principal: Principal, package_id: int) -> list[dict[str, Any]]:
        package = self._require_package(package_id)
        principal.require("handoff.read")
        self._require_can_view(principal, package)
        rows = self.connection.execute(
            "SELECT * FROM handoff_records WHERE package_id=? ORDER BY seq",
            (package_id,),
        ).fetchall()
        return [self._record_view(dict(row)) for row in rows]

    def version_detail(self, principal: Principal, package_id: int, version_no: int) -> dict[str, Any]:
        package = self._require_package(package_id)
        principal.require("handoff.read")
        self._require_can_view(principal, package)
        row = self.connection.execute(
            "SELECT * FROM handoff_versions WHERE package_id=? AND version_no=?",
            (package_id, version_no),
        ).fetchone()
        if row is None:
            raise NotFoundError("行动包版本不存在")
        materials = json.loads(row["materials_json"])
        return {
            "package_id": package_id,
            "version_no": row["version_no"],
            "manifest_digest": row["manifest_digest"],
            "digest_verified": manifest_digest(materials) == row["manifest_digest"],
            "materials": materials,
            "created_by_name": row["created_by_name"],
            "remark": row["remark"],
            "created_at": row["created_at"],
        }

    def list_overdue(self, principal: Principal) -> list[dict[str, Any]]:
        principal.require("handoff.read")
        now_text = to_storage(self.clock.now())
        rows = self.connection.execute(
            "SELECT r.* FROM handoff_records r JOIN handoff_packages p ON p.id=r.package_id "
            "WHERE r.status IN ('pending','partial') AND r.deadline_at<=? AND p.locked=0 ORDER BY r.deadline_at",
            (now_text,),
        ).fetchall()
        result = []
        for row in rows:
            item = self._record_view(dict(row))
            if not self._can_view_department(principal, item["sender_department_id"], item["receiver_department_id"]):
                continue
            result.append(item)
        return result

    # ---------------------------------------------------------------- 内部方法

    def _insert_version(
        self,
        package_id: int,
        version_no: int,
        materials: list[dict[str, Any]],
        principal: Principal,
        remark: str,
        now_text: str,
        *,
        revised_from_record_id: int | None = None,
    ) -> None:
        digest = manifest_digest(materials)
        self.connection.execute(
            "INSERT INTO handoff_versions(package_id,version_no,materials_json,manifest_digest,"
            "revised_from_record_id,created_by_user_id,created_by_name,remark,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (
                package_id,
                version_no,
                json.dumps(materials, ensure_ascii=False, sort_keys=True),
                digest,
                revised_from_record_id,
                principal.user_id,
                principal.display_name,
                remark.strip(),
                now_text,
            ),
        )

    def _insert_record(
        self,
        package_id: int,
        *,
        seq: int,
        version_no: int,
        sender: Principal,
        receiver_department_id: int,
        deadline_text: str,
        note: str,
        now_text: str,
        dispatched_by: Principal | None = None,
    ) -> None:
        self.connection.execute(
            "INSERT INTO handoff_records(package_id,seq,version_no,sender_user_id,sender_name,"
            "sender_department_id,receiver_department_id,status,deadline_at,dispatched_by_user_id,"
            "dispatched_by_name,note,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                package_id,
                seq,
                version_no,
                sender.user_id,
                sender.display_name,
                sender.department_id,
                receiver_department_id,
                RECORD_PENDING,
                deadline_text,
                dispatched_by.user_id if dispatched_by else None,
                dispatched_by.display_name if dispatched_by else "",
                note.strip(),
                now_text,
            ),
        )

    @staticmethod
    def _normalize_material(item: Any) -> dict[str, Any]:
        payload = item if isinstance(item, dict) else item.model_dump()
        fields = [str(field).strip() for field in payload.get("sensitive_fields", []) if str(field).strip()]
        return {
            "key": payload["key"].strip(),
            "kind": payload["kind"],
            "title": payload["title"].strip(),
            "content": payload.get("content", ""),
            "sensitive": bool(payload.get("sensitive")) or bool(fields),
            "sensitive_fields": fields,
        }

    def _version_materials(self, package_id: int, version_no: int) -> list[dict[str, Any]]:
        row = self.connection.execute(
            "SELECT materials_json FROM handoff_versions WHERE package_id=? AND version_no=?",
            (package_id, version_no),
        ).fetchone()
        if row is None:
            raise NotFoundError("行动包版本不存在")
        return json.loads(row["materials_json"])

    def _require_package(self, package_id: int) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM handoff_packages WHERE id=?", (package_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("行动包不存在")
        return dict(row)

    def _require_last_record(self, package_id: int) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM handoff_records WHERE package_id=? ORDER BY seq DESC LIMIT 1",
            (package_id,),
        ).fetchone()
        if row is None:
            raise ConflictError("行动包缺少交接记录")
        return dict(row)

    def _require_active_record(self, package_id: int) -> dict[str, Any]:
        record = self._require_last_record(package_id)
        if record["status"] in {
            RECORD_ACCEPTED,
            RECORD_RETURNED,
            RECORD_FORWARDED,
            RECORD_REASSIGNED,
            RECORD_COMPLETED,
        }:
            raise ConflictError("当前交接环节已经结束，请查看最新交接记录")
        return record

    def _require_unlocked(self, package: dict[str, Any], principal: Principal) -> None:
        if int(package["locked"]) == 1 or package["status"] == PACKAGE_STATUS_COMPLETED:
            raise ConflictError("行动包已完成并封存，不得修改")

    def ensure_mutable(self, package_id: int, principal: Principal, *, permission: str = "handoff.write") -> None:
        """在开启写事务前做封存预检并独立留痕。

        必须在自动提交连接上调用：随后业务事务会以 IMMEDIATE 方式开启，
        若在事务内记录拒绝审计，会随冲突回滚而丢失。
        """
        principal.require(permission)
        package = self._require_package(package_id)
        if int(package["locked"]) == 1 or package["status"] == PACKAGE_STATUS_COMPLETED:
            self.audit.record(
                AuditContext(principal.user_id, principal.display_name),
                action="handoff.locked.rejected",
                resource_type="handoff_package",
                resource_id=package["id"],
                outcome="denied",
                metadata={"reason": "行动包已完成并封存，禁止修改"},
            )
            raise ConflictError("行动包已完成并封存，不得修改")

    def _require_active_department(self, department_id: int) -> None:
        row = self.connection.execute(
            "SELECT id FROM departments WHERE id=? AND is_active=1", (department_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("接收部门不存在或已停用")

    def _require_receiver(self, principal: Principal, record: dict[str, Any]) -> None:
        if "*" in principal.permissions:
            return
        if "handoff.write" not in principal.permissions:
            raise PermissionDeniedError("缺少权限：handoff.write")
        if principal.department_id != int(record["receiver_department_id"]):
            raise PermissionDeniedError("只有当前接收部门可以处理该交接")

    def _require_initiator(self, principal: Principal, package: dict[str, Any]) -> None:
        if "*" in principal.permissions:
            return
        if "handoff.write" not in principal.permissions:
            raise PermissionDeniedError("缺少权限：handoff.write")
        if principal.department_id is not None and principal.department_id == package.get("initiator_department_id"):
            return
        if principal.user_id == package.get("initiator_user_id"):
            return
        raise PermissionDeniedError("只有行动包发起方可以修订并重新提交")

    def _require_department_scope(self, principal: Principal, department_id: int) -> None:
        if self._has_global_visibility(principal):
            return
        if principal.department_id != department_id:
            raise PermissionDeniedError("不能查询其他部门的交接")

    @staticmethod
    def _has_global_visibility(principal: Principal) -> bool:
        # 管理员与值班调度员需要跨部门视角发现超时交接
        return "*" in principal.permissions or "handoff.dispatch" in principal.permissions

    def _can_view_department(self, principal: Principal, sender_id: Any, receiver_id: Any) -> bool:
        if self._has_global_visibility(principal):
            return True
        if principal.department_id is not None and principal.department_id in {sender_id, receiver_id}:
            return True
        return False

    def _require_can_view(self, principal: Principal, package: dict[str, Any]) -> None:
        if self._has_global_visibility(principal):
            return
        if package.get("initiator_user_id") == principal.user_id:
            return
        if principal.department_id is not None and (
            principal.department_id == package.get("initiator_department_id")
            or self.connection.execute(
                "SELECT 1 FROM handoff_records WHERE package_id=? AND receiver_department_id=? LIMIT 1",
                (package["id"], principal.department_id),
            ).fetchone()
        ):
            return
        raise PermissionDeniedError("无权查看该行动包")

    def _package_summary(self, package: dict[str, Any]) -> dict[str, Any]:
        active = self.connection.execute(
            "SELECT * FROM handoff_records WHERE package_id=? ORDER BY seq DESC LIMIT 1",
            (package["id"],),
        ).fetchone()
        summary = {
            "id": package["id"],
            "code": package["code"],
            "title": package["title"],
            "subject": package["subject"],
            "status": package["status"],
            "current_version_no": package["current_version_no"],
            "locked": bool(package["locked"]),
            "initiator_name": package["initiator_name"],
            "created_at": package["created_at"],
            "completed_at": package["completed_at"],
        }
        if active is not None:
            view = self._record_view(dict(active))
            summary["active_handoff"] = {key: view[key] for key in (
                "seq", "status", "receiver_department_id", "deadline_at", "is_overdue",
            )}
        return summary

    def _record_view(self, record: dict[str, Any]) -> dict[str, Any]:
        acks = [
            dict(row)
            for row in self.connection.execute(
                "SELECT material_key,accepted,note,acked_by_name,acked_at FROM handoff_item_acks "
                "WHERE handoff_record_id=? ORDER BY id",
                (record["id"],),
            ).fetchall()
        ]
        for ack in acks:
            ack["accepted"] = bool(ack["accepted"])
        receiver = self.connection.execute(
            "SELECT name FROM departments WHERE id=?", (record["receiver_department_id"],)
        ).fetchone()
        overdue = (
            record["status"] in {RECORD_PENDING, RECORD_PARTIAL}
            and from_storage(record["deadline_at"]) is not None
            and from_storage(record["deadline_at"]) <= self.clock.now()
        )
        return {
            "id": record["id"],
            "package_id": record["package_id"],
            "seq": record["seq"],
            "version_no": record["version_no"],
            "sender_name": record["sender_name"],
            "sender_department_id": record["sender_department_id"],
            "receiver_department_id": record["receiver_department_id"],
            "receiver_department_name": receiver["name"] if receiver else None,
            "status": record["status"],
            "deadline_at": record["deadline_at"],
            "acknowledged_at": record["acknowledged_at"],
            "return_reason": record["return_reason"],
            "dispatched_by_name": record["dispatched_by_name"],
            "note": record["note"],
            "created_at": record["created_at"],
            "is_overdue": overdue,
            "item_acks": acks,
        }

    def _is_overdue(self, package: dict[str, Any]) -> bool:
        row = self.connection.execute(
            "SELECT status,deadline_at FROM handoff_records WHERE package_id=? ORDER BY seq DESC LIMIT 1",
            (package["id"],),
        ).fetchone()
        if row is None or row["status"] not in {RECORD_PENDING, RECORD_PARTIAL}:
            return False
        deadline = from_storage(row["deadline_at"])
        return deadline is not None and deadline <= self.clock.now()
