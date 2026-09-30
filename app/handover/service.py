from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import timedelta
from typing import Any

from app.core.clock import Clock, SystemClock, to_storage
from app.core.errors import ConflictError, NotFoundError, PermissionDeniedError, ValidationError
from app.core.security import Principal
from app.services.access import DataScope
from app.services.audit import AuditContext, AuditService

SCHEMA = """
CREATE TABLE IF NOT EXISTS handover_packages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    incident_ref TEXT NOT NULL DEFAULT '',
    owner_department_id INTEGER REFERENCES departments(id),
    created_by_user_id INTEGER,
    created_by_name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT '草稿' CHECK(status IN ('草稿','交接中','修订中','已完成')),
    current_handover_id INTEGER,
    archived_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS handover_materials (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    package_id INTEGER NOT NULL REFERENCES handover_packages(id) ON DELETE CASCADE,
    item_key TEXT NOT NULL,
    title TEXT NOT NULL,
    material_type TEXT NOT NULL,
    content_json TEXT NOT NULL DEFAULT '{}',
    is_sensitive INTEGER NOT NULL DEFAULT 0 CHECK(is_sensitive IN (0,1)),
    sensitive_fields_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(package_id, item_key)
);

CREATE TABLE IF NOT EXISTS handover_handovers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    package_id INTEGER NOT NULL REFERENCES handover_packages(id) ON DELETE CASCADE,
    sequence_no INTEGER NOT NULL,
    from_department_id INTEGER,
    to_department_id INTEGER NOT NULL,
    dispatched_by_user_id INTEGER,
    dispatched_by_name TEXT NOT NULL,
    deadline_at TEXT NOT NULL,
    dispatched_at TEXT NOT NULL,
    UNIQUE(package_id, sequence_no)
);

CREATE TABLE IF NOT EXISTS handover_receipts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    handover_id INTEGER NOT NULL REFERENCES handover_handovers(id) ON DELETE CASCADE,
    package_id INTEGER NOT NULL REFERENCES handover_packages(id) ON DELETE CASCADE,
    item_key TEXT NOT NULL,
    decision TEXT NOT NULL CHECK(decision IN ('accepted','rejected')),
    comment TEXT NOT NULL DEFAULT '',
    receiver_user_id INTEGER,
    receiver_name TEXT NOT NULL,
    is_overdue INTEGER NOT NULL DEFAULT 0 CHECK(is_overdue IN (0,1)),
    received_at TEXT NOT NULL,
    UNIQUE(handover_id, item_key)
);

CREATE TABLE IF NOT EXISTS handover_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    handover_id INTEGER REFERENCES handover_handovers(id) ON DELETE CASCADE,
    package_id INTEGER NOT NULL REFERENCES handover_packages(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL CHECK(event_type IN
        ('dispatched','item_received','package_returned','taken_over','completed')),
    actor_user_id INTEGER,
    actor_name TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS handover_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    package_id INTEGER NOT NULL REFERENCES handover_packages(id) ON DELETE CASCADE,
    version_no INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('created','dispatched','corrected','completed')),
    handover_id INTEGER,
    payload_json TEXT NOT NULL,
    prev_digest TEXT NOT NULL DEFAULT '',
    digest TEXT NOT NULL,
    created_by_name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(package_id, version_no)
);

CREATE TABLE IF NOT EXISTS handover_corrections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    package_id INTEGER NOT NULL REFERENCES handover_packages(id) ON DELETE CASCADE,
    version_no INTEGER NOT NULL,
    reason TEXT NOT NULL,
    changed_item_keys_json TEXT NOT NULL DEFAULT '[]',
    actor_user_id INTEGER,
    actor_name TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_handover_packages_status ON handover_packages(status, id);
CREATE INDEX IF NOT EXISTS idx_handover_materials_package ON handover_materials(package_id, item_key);
CREATE INDEX IF NOT EXISTS idx_handover_handovers_package ON handover_handovers(package_id, sequence_no);
CREATE INDEX IF NOT EXISTS idx_handover_receipts_handover ON handover_receipts(handover_id, item_key);
CREATE INDEX IF NOT EXISTS idx_handover_events_package ON handover_events(package_id, id);

CREATE TRIGGER IF NOT EXISTS handover_versions_no_update
BEFORE UPDATE ON handover_versions
BEGIN SELECT RAISE(ABORT, '行动包版本记录禁止修改'); END;
CREATE TRIGGER IF NOT EXISTS handover_versions_no_delete
BEFORE DELETE ON handover_versions
BEGIN SELECT RAISE(ABORT, '行动包版本记录禁止删除'); END;
CREATE TRIGGER IF NOT EXISTS handover_handovers_no_update
BEFORE UPDATE ON handover_handovers
BEGIN SELECT RAISE(ABORT, '交接责任记录禁止修改'); END;
CREATE TRIGGER IF NOT EXISTS handover_handovers_no_delete
BEFORE DELETE ON handover_handovers
BEGIN SELECT RAISE(ABORT, '交接责任记录禁止删除'); END;
CREATE TRIGGER IF NOT EXISTS handover_receipts_no_update
BEFORE UPDATE ON handover_receipts
BEGIN SELECT RAISE(ABORT, '接收确认记录禁止修改'); END;
CREATE TRIGGER IF NOT EXISTS handover_receipts_no_delete
BEFORE DELETE ON handover_receipts
BEGIN SELECT RAISE(ABORT, '接收确认记录禁止删除'); END;
CREATE TRIGGER IF NOT EXISTS handover_events_no_update
BEFORE UPDATE ON handover_events
BEGIN SELECT RAISE(ABORT, '交接过程记录禁止修改'); END;
CREATE TRIGGER IF NOT EXISTS handover_events_no_delete
BEFORE DELETE ON handover_events
BEGIN SELECT RAISE(ABORT, '交接过程记录禁止删除'); END;
"""

EDITABLE_STATUSES = {"草稿", "修订中"}


def default_clock() -> Clock:
    return SystemClock()


def ensure_schema() -> None:
    from app.database import get_connection

    get_connection().executescript(SCHEMA)


def _canonical(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest_payload(*parts: Any) -> str:
    joined = "|".join(_canonical(part) for part in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def _mask_content(content: Any, sensitive: bool, fields: list[str]) -> Any:
    if not sensitive:
        return content
    if not fields:
        return {"_redacted": True}
    masked = json.loads(json.dumps(content, ensure_ascii=False)) if isinstance(content, (dict, list)) else content
    if not isinstance(masked, dict):
        return {"_redacted": True}
    for path in fields:
        node: Any = masked
        segments = path.split(".")
        for segment in segments[:-1]:
            if not isinstance(node, dict) or segment not in node:
                node = None
                break
            node = node[segment]
        if isinstance(node, dict) and segments[-1] in node:
            node[segments[-1]] = "******"
    return masked


class HandoverService:
    """跨部门行动包与交接链。"""

    def __init__(self, connection: sqlite3.Connection, clock: Clock | None = None) -> None:
        self.connection = connection
        self.clock = clock or default_clock()
        self.audit = AuditService(connection, self.clock)

    # ------------------------------------------------------------------ 内部工具

    def _actor(self, principal: Principal) -> AuditContext:
        return AuditContext(principal.user_id, principal.display_name)

    def _require_package(self, package_id: int) -> dict:
        package = self.connection.execute("SELECT * FROM handover_packages WHERE id=?", (package_id,)).fetchone()
        if package is None:
            raise NotFoundError("行动包不存在")
        return dict(package)

    def _require_active_department(self, department_id: int) -> dict:
        row = self.connection.execute("SELECT * FROM departments WHERE id=? AND is_active=1", (department_id,)).fetchone()
        if row is None:
            raise NotFoundError("部门不存在或已停用")
        return dict(row)

    def _materials(self, package_id: int) -> list[dict]:
        rows = self.connection.execute(
            "SELECT * FROM handover_materials WHERE package_id=? ORDER BY id", (package_id,)
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["content"] = json.loads(item.pop("content_json"))
            item["sensitive"] = bool(item.pop("is_sensitive"))
            item["sensitive_fields"] = json.loads(item.pop("sensitive_fields_json"))
            result.append(item)
        return result

    def _participant_departments(self, package_id: int) -> set[int | None]:
        package = self._require_package(package_id)
        departments = {package["owner_department_id"]}
        rows = self.connection.execute(
            "SELECT from_department_id, to_department_id FROM handover_handovers WHERE package_id=?",
            (package_id,),
        ).fetchall()
        for row in rows:
            departments.add(row["from_department_id"])
            departments.add(row["to_department_id"])
        return departments

    def _require_participant(self, principal: Principal, permission: str, package_id: int) -> DataScope:
        scope = DataScope.from_principal(principal, permission)
        if scope.mode == "all":
            return scope
        if principal.can("handover.duty"):
            return DataScope("all", None)
        if scope.mode != "department" or principal.department_id not in self._participant_departments(package_id):
            raise PermissionDeniedError("该行动包不在当前账号的部门范围内")
        return scope

    def _current_handover(self, package: dict) -> dict | None:
        if package["current_handover_id"] is None:
            return None
        row = self.connection.execute(
            "SELECT * FROM handover_handovers WHERE id=?", (package["current_handover_id"],)
        ).fetchone()
        return dict(row) if row else None

    def _handover_state(self, handover_id: int) -> str:
        row = self.connection.execute(
            "SELECT event_type FROM handover_events WHERE handover_id=? "
            "AND event_type IN ('package_returned','taken_over','completed') LIMIT 1",
            (handover_id,),
        ).fetchone()
        return row["event_type"] if row else "dispatched"

    def _receipts_by_key(self, handover_id: int) -> dict[str, dict]:
        rows = self.connection.execute(
            "SELECT * FROM handover_receipts WHERE handover_id=?", (handover_id,)
        ).fetchall()
        return {row["item_key"]: dict(row) for row in rows}

    def _snapshot(self, package: dict, handover: dict | None) -> dict:
        return {
            "package": {
                "id": package["id"],
                "title": package["title"],
                "description": package["description"],
                "incident_ref": package["incident_ref"],
                "status": package["status"],
                "owner_department_id": package["owner_department_id"],
            },
            "materials": self._materials(package["id"]),
            "handover": (
                None
                if handover is None
                else {
                    "sequence_no": handover["sequence_no"],
                    "from_department_id": handover["from_department_id"],
                    "to_department_id": handover["to_department_id"],
                    "deadline_at": handover["deadline_at"],
                    "dispatched_at": handover["dispatched_at"],
                }
            ),
        }

    def _append_version(
        self,
        package: dict,
        kind: str,
        actor_name: str,
        now: str,
        *,
        handover_id: int | None = None,
    ) -> dict:
        last = self.connection.execute(
            "SELECT version_no, digest FROM handover_versions WHERE package_id=? ORDER BY version_no DESC LIMIT 1",
            (package["id"],),
        ).fetchone()
        version_no = 1 if last is None else int(last["version_no"]) + 1
        prev_digest = "" if last is None else last["digest"]
        handover = self._current_handover(package) if handover_id is None else self._handover_row(handover_id)
        payload = self._snapshot(package, handover)
        digest = digest_payload(prev_digest, version_no, kind, actor_name, now, payload)
        cursor = self.connection.execute(
            "INSERT INTO handover_versions(package_id,version_no,kind,handover_id,payload_json,"
            "prev_digest,digest,created_by_name,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                package["id"],
                version_no,
                kind,
                handover_id if handover_id is not None else package["current_handover_id"],
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                prev_digest,
                digest,
                actor_name,
                now,
            ),
        )
        return {"id": cursor.lastrowid, "version_no": version_no, "digest": digest, "prev_digest": prev_digest}

    def _handover_row(self, handover_id: int) -> dict | None:
        row = self.connection.execute("SELECT * FROM handover_handovers WHERE id=?", (handover_id,)).fetchone()
        return dict(row) if row else None

    def _event(
        self,
        package_id: int,
        handover_id: int | None,
        event_type: str,
        principal: Principal,
        now: str,
        payload: dict | None = None,
    ) -> None:
        self.connection.execute(
            "INSERT INTO handover_events(handover_id,package_id,event_type,actor_user_id,actor_name,payload_json,created_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (
                handover_id,
                package_id,
                event_type,
                principal.user_id,
                principal.display_name,
                json.dumps(payload or {}, ensure_ascii=False, sort_keys=True),
                now,
            ),
        )

    def _serialize_materials(self, package: dict, can_view_sensitive: bool) -> list[dict]:
        materials = self._materials(package["id"])
        handover = self._current_handover(package)
        receipts = self._receipts_by_key(handover["id"]) if handover else {}
        for item in materials:
            receipt = receipts.get(item["item_key"])
            item["receipt"] = None if receipt is None else {
                "decision": receipt["decision"],
                "comment": receipt["comment"],
                "receiver_name": receipt["receiver_name"],
                "received_at": receipt["received_at"],
                "is_overdue": bool(receipt["is_overdue"]),
            }
            if not can_view_sensitive and item["sensitive"]:
                item["content"] = _mask_content(item["content"], True, item["sensitive_fields"])
        return materials

    def _can_view_sensitive(self, principal: Principal, package: dict) -> bool:
        if "*" in principal.permissions:
            return True
        if principal.department_id is None:
            return False
        if principal.department_id == package["owner_department_id"]:
            return True
        handover = self._current_handover(package)
        return bool(handover and handover["to_department_id"] == principal.department_id)

    def _mask_snapshot(self, payload: dict, can_view: bool) -> dict:
        if can_view:
            return payload
        masked = json.loads(json.dumps(payload, ensure_ascii=False))
        for item in masked.get("materials", []):
            if item.get("sensitive"):
                item["content"] = _mask_content(item.get("content", {}), True, item.get("sensitive_fields", []))
        return masked

    def _serialize_package(self, package: dict, principal: Principal) -> dict:
        result = dict(package)
        handover = self._current_handover(package)
        receipts = self._receipts_by_key(handover["id"]) if handover else None
        materials = self._materials(package["id"])
        accepted = sum(1 for item in materials if (receipts or {}).get(item["item_key"], {}).get("decision") == "accepted")
        rejected = sum(1 for item in materials if (receipts or {}).get(item["item_key"], {}).get("decision") == "rejected")
        can_view = self._can_view_sensitive(principal, package)
        result["materials"] = self._serialize_materials(package, can_view)
        result["receipt_summary"] = {
            "total": len(materials),
            "accepted": accepted,
            "rejected": rejected,
            "pending": len(materials) - accepted - rejected,
        }
        if handover:
            now = self.clock.now()
            from app.core.clock import from_storage

            deadline = from_storage(handover["deadline_at"])
            result["current_handover"] = {
                "id": handover["id"],
                "sequence_no": handover["sequence_no"],
                "from_department_id": handover["from_department_id"],
                "to_department_id": handover["to_department_id"],
                "dispatched_by_name": handover["dispatched_by_name"],
                "dispatched_at": handover["dispatched_at"],
                "deadline_at": handover["deadline_at"],
                "state": self._handover_state(handover["id"]),
                "is_overdue": bool(deadline and deadline < now and self._handover_state(handover["id"]) == "dispatched"),
            }
        else:
            result["current_handover"] = None
        result.pop("current_handover_id", None)
        return result

    # ------------------------------------------------------------------ 行动包

    def create_package(self, principal: Principal, data: dict) -> dict:
        principal.require("handover.manage")
        owner_id = data.get("owner_department_id")
        if owner_id is None:
            owner_id = principal.department_id
        if owner_id is None:
            raise ValidationError("发起人必须属于一个部门，或显式指定发起部门")
        if "*" not in principal.permissions and owner_id != principal.department_id:
            raise PermissionDeniedError("不能代表其他部门发起行动包")
        self._require_active_department(owner_id)
        now = to_storage(self.clock.now())
        cursor = self.connection.execute(
            "INSERT INTO handover_packages(title,description,incident_ref,owner_department_id,"
            "created_by_user_id,created_by_name,status,created_at,updated_at) VALUES(?,?,?,?,?,?,'草稿',?,?)",
            (
                data["title"].strip(),
                data.get("description", "").strip(),
                data.get("incident_ref", "").strip(),
                owner_id,
                principal.user_id,
                principal.display_name,
                now,
                now,
            ),
        )
        package_id = int(cursor.lastrowid)
        for item in data["materials"]:
            self._insert_material(package_id, item, now)
        package = self._require_package(package_id)
        version = self._append_version(package, "created", principal.display_name, now)
        self.audit.record(
            self._actor(principal),
            action="handover.create",
            resource_type="handover_package",
            resource_id=package_id,
            after={"title": package["title"], "version_no": version["version_no"], "materials": len(data["materials"])},
        )
        return self.detail(principal, package_id)

    def _insert_material(self, package_id: int, item: dict, now: str) -> None:
        self.connection.execute(
            "INSERT INTO handover_materials(package_id,item_key,title,material_type,content_json,"
            "is_sensitive,sensitive_fields_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                package_id,
                item["item_key"],
                item["title"].strip(),
                item["material_type"],
                json.dumps(item.get("content", {}), ensure_ascii=False, sort_keys=True),
                1 if item.get("sensitive", False) else 0,
                json.dumps(item.get("sensitive_fields", []), ensure_ascii=False),
                now,
                now,
            ),
        )

    def list_packages(
        self,
        principal: Principal,
        *,
        status: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[int, list[dict]]:
        scope = DataScope.from_principal(principal, "handover.read")
        if scope.mode != "all" and principal.can("handover.duty"):
            scope = DataScope("all", None)
        conditions: list[str] = []
        params: list[Any] = []
        if status:
            conditions.append("p.status=?")
            params.append(status)
        if scope.mode == "self":
            conditions.append("1=0")
        elif scope.mode == "department":
            conditions.append(
                "(p.owner_department_id=? OR EXISTS(SELECT 1 FROM handover_handovers h "
                "WHERE h.package_id=p.id AND (h.from_department_id=? OR h.to_department_id=?)))"
            )
            params.extend([principal.department_id, principal.department_id, principal.department_id])
        where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
        total = int(self.connection.execute(f"SELECT COUNT(*) FROM handover_packages p{where}", tuple(params)).fetchone()[0])
        params.extend([limit, offset])
        rows = self.connection.execute(
            f"SELECT p.* FROM handover_packages p{where} ORDER BY p.id DESC LIMIT ? OFFSET ?", tuple(params)
        ).fetchall()
        return total, [self._serialize_package(dict(row), principal) for row in rows]

    def detail(self, principal: Principal, package_id: int) -> dict:
        package = self._require_package(package_id)
        self._require_participant(principal, "handover.read", package_id)
        return self._serialize_package(package, principal)

    def update_package(self, principal: Principal, package_id: int, changes: dict) -> dict:
        package = self._require_package(package_id)
        self._require_owner(principal, package)
        if package["status"] == "已完成":
            raise ConflictError("已完成的行动包不得直接修改，请使用留痕更正")
        if package["status"] not in EDITABLE_STATUSES:
            raise ConflictError("行动包处于交接中，不能直接修改；请由接收方退回或使用留痕更正")
        allowed = {key: value.strip() for key, value in changes.items() if key in {"title", "description", "incident_ref"} and value is not None}
        if not allowed:
            raise ValidationError("没有可更新的字段")
        assignments = ",".join(f"{key}=?" for key in allowed)
        now = to_storage(self.clock.now())
        self.connection.execute(
            f"UPDATE handover_packages SET {assignments},updated_at=? WHERE id=?",
            (*allowed.values(), now, package_id),
        )
        self.audit.record(
            self._actor(principal),
            action="handover.update",
            resource_type="handover_package",
            resource_id=package_id,
            before={key: package[key] for key in allowed},
            after=allowed,
        )
        return self.detail(principal, package_id)

    def _require_owner(self, principal: Principal, package: dict) -> DataScope:
        scope = DataScope.from_principal(principal, "handover.manage")
        if scope.mode != "all" and package["owner_department_id"] != principal.department_id:
            raise PermissionDeniedError("只有发起部门可以维护行动包材料")
        return scope

    def add_material(self, principal: Principal, package_id: int, item: dict) -> dict:
        package = self._require_package(package_id)
        self._require_owner(principal, package)
        if package["status"] not in EDITABLE_STATUSES:
            raise ConflictError("当前状态不能直接追加材料，请使用留痕更正")
        now = to_storage(self.clock.now())
        try:
            self._insert_material(package_id, item, now)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("材料标识已存在") from exc
        self.connection.execute("UPDATE handover_packages SET updated_at=? WHERE id=?", (now, package_id))
        self.audit.record(
            self._actor(principal),
            action="handover.material.add",
            resource_type="handover_package",
            resource_id=package_id,
            after={"item_key": item["item_key"]},
        )
        return self.detail(principal, package_id)

    def update_material(self, principal: Principal, package_id: int, item_key: str, changes: dict) -> dict:
        package = self._require_package(package_id)
        self._require_owner(principal, package)
        if package["status"] not in EDITABLE_STATUSES:
            raise ConflictError("材料已转交，不能直接修改；请退回修订或使用留痕更正")
        row = self.connection.execute(
            "SELECT * FROM handover_materials WHERE package_id=? AND item_key=?", (package_id, item_key)
        ).fetchone()
        if row is None:
            raise NotFoundError("材料不存在")
        current = dict(row)
        title = changes["title"].strip() if changes.get("title") is not None else current["title"]
        content = changes["content"] if changes.get("content") is not None else json.loads(current["content_json"])
        sensitive = changes["sensitive"] if changes.get("sensitive") is not None else bool(current["is_sensitive"])
        fields = changes["sensitive_fields"] if changes.get("sensitive_fields") is not None else json.loads(current["sensitive_fields_json"])
        now = to_storage(self.clock.now())
        self.connection.execute(
            "UPDATE handover_materials SET title=?,content_json=?,is_sensitive=?,sensitive_fields_json=?,updated_at=?"
            " WHERE package_id=? AND item_key=?",
            (
                title,
                json.dumps(content, ensure_ascii=False, sort_keys=True),
                1 if sensitive else 0,
                json.dumps(fields, ensure_ascii=False),
                now,
                package_id,
                item_key,
            ),
        )
        self.audit.record(
            self._actor(principal),
            action="handover.material.update",
            resource_type="handover_package",
            resource_id=package_id,
            before={"item_key": item_key, "content_hash": digest_payload(json.loads(current["content_json"]))},
            after={"item_key": item_key, "content_hash": digest_payload(content)},
        )
        return self.detail(principal, package_id)

    def delete_material(self, principal: Principal, package_id: int, item_key: str) -> None:
        package = self._require_package(package_id)
        self._require_owner(principal, package)
        if package["status"] != "草稿":
            raise ConflictError("行动包一旦转交，材料只能通过留痕更正处理，不能删除")
        cursor = self.connection.execute(
            "DELETE FROM handover_materials WHERE package_id=? AND item_key=?", (package_id, item_key)
        )
        if cursor.rowcount == 0:
            raise NotFoundError("材料不存在")
        self.audit.record(
            self._actor(principal),
            action="handover.material.delete",
            resource_type="handover_package",
            resource_id=package_id,
            before={"item_key": item_key},
        )

    # ------------------------------------------------------------------ 交接链

    def dispatch(self, principal: Principal, package_id: int, to_department_id: int, deadline_hours: int) -> dict:
        package = self._require_package(package_id)
        self._require_owner(principal, package)
        if package["status"] not in EDITABLE_STATUSES:
            raise ConflictError("行动包当前状态不能转交")
        materials = self._materials(package_id)
        if not materials:
            raise ValidationError("行动包没有材料，不能转交")
        if to_department_id == package["owner_department_id"]:
            raise ValidationError("不能转交给发起部门自身")
        self._require_active_department(to_department_id)
        last_sequence = self.connection.execute(
            "SELECT COALESCE(MAX(sequence_no),0) AS seq FROM handover_handovers WHERE package_id=?", (package_id,)
        ).fetchone()["seq"]
        now_dt = self.clock.now()
        now = to_storage(now_dt)
        deadline = to_storage(now_dt + timedelta(hours=deadline_hours))
        cursor = self.connection.execute(
            "INSERT INTO handover_handovers(package_id,sequence_no,from_department_id,to_department_id,"
            "dispatched_by_user_id,dispatched_by_name,deadline_at,dispatched_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                package_id,
                int(last_sequence) + 1,
                package["owner_department_id"],
                to_department_id,
                principal.user_id,
                principal.display_name,
                deadline,
                now,
            ),
        )
        handover_id = int(cursor.lastrowid)
        self.connection.execute(
            "UPDATE handover_packages SET status='交接中',current_handover_id=?,archived_at=NULL,updated_at=? WHERE id=?",
            (handover_id, now, package_id),
        )
        package = self._require_package(package_id)
        self._event(package_id, handover_id, "dispatched", principal, now, {"deadline_at": deadline, "deadline_hours": deadline_hours})
        version = self._append_version(package, "dispatched", principal.display_name, now, handover_id=handover_id)
        self.audit.record(
            self._actor(principal),
            action="handover.dispatch",
            resource_type="handover_package",
            resource_id=package_id,
            after={
                "handover_id": handover_id,
                "sequence_no": int(last_sequence) + 1,
                "to_department_id": to_department_id,
                "deadline_at": deadline,
                "version_no": version["version_no"],
            },
        )
        return self.detail(principal, package_id)

    def _require_receiver(self, principal: Principal, handover: dict) -> None:
        scope = DataScope.from_principal(principal, "handover.manage")
        if scope.mode == "all":
            return
        if scope.mode != "department" or principal.department_id != handover["to_department_id"]:
            raise PermissionDeniedError("只有当前接收部门可以确认或退回")

    def receive(self, principal: Principal, package_id: int, decisions: list[dict]) -> dict:
        package = self._require_package(package_id)
        handover = self._current_handover(package)
        if handover is None:
            raise ConflictError("行动包尚未转交")
        self._require_receiver(principal, handover)
        if package["status"] != "交接中" or self._handover_state(handover["id"]) != "dispatched":
            raise ConflictError("当前交接环节不接收材料确认")
        material_keys = {item["item_key"] for item in self._materials(package_id)}
        submitted = {}
        for entry in decisions:
            key = entry["item_key"].strip()
            if key not in material_keys:
                raise NotFoundError(f"材料不存在：{key}")
            if key in submitted:
                raise ValidationError(f"材料在本次提交中重复：{key}")
            submitted[key] = entry
        existing = self._receipts_by_key(handover["id"])
        for key, entry in submitted.items():
            prior = existing.get(key)
            if prior is not None:
                raise ConflictError(f"材料 {key} 已由 {prior['receiver_name']} 确认（{prior['decision']}），不能重复确认")
        from app.core.clock import from_storage

        now_dt = self.clock.now()
        now = to_storage(now_dt)
        overdue = bool(from_storage(handover["deadline_at"]) and now_dt > from_storage(handover["deadline_at"]))
        for key, entry in submitted.items():
            self.connection.execute(
                "INSERT INTO handover_receipts(handover_id,package_id,item_key,decision,comment,"
                "receiver_user_id,receiver_name,is_overdue,received_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    handover["id"],
                    package_id,
                    key,
                    entry["decision"],
                    entry.get("comment", "").strip(),
                    principal.user_id,
                    principal.display_name,
                    1 if overdue else 0,
                    now,
                ),
            )
        self._event(
            package_id,
            handover["id"],
            "item_received",
            principal,
            now,
            {"decisions": {key: entry["decision"] for key, entry in submitted.items()}, "is_overdue": overdue},
        )
        self.audit.record(
            self._actor(principal),
            action="handover.receive",
            resource_type="handover_package",
            resource_id=package_id,
            after={"handover_id": handover["id"], "decisions": {key: entry["decision"] for key, entry in submitted.items()}, "is_overdue": overdue},
        )
        self._maybe_complete(principal, package, handover, now)
        return self.detail(principal, package_id)

    def _maybe_complete(self, principal: Principal, package: dict, handover: dict, now: str) -> None:
        materials = self._materials(package["id"])
        receipts = self._receipts_by_key(handover["id"])
        if len(materials) > 0 and all(receipts.get(item["item_key"], {}).get("decision") == "accepted" for item in materials):
            self.connection.execute(
                "UPDATE handover_packages SET status='已完成',archived_at=?,updated_at=? WHERE id=?",
                (now, now, package["id"]),
            )
            package = self._require_package(package["id"])
            self._event(package["id"], handover["id"], "completed", principal, now)
            version = self._append_version(package, "completed", principal.display_name, now, handover_id=handover["id"])
            self.audit.record(
                self._actor(principal),
                action="handover.complete",
                resource_type="handover_package",
                resource_id=package["id"],
                after={"handover_id": handover["id"], "version_no": version["version_no"]},
            )

    def return_package(self, principal: Principal, package_id: int, reason: str) -> dict:
        package = self._require_package(package_id)
        handover = self._current_handover(package)
        if handover is None:
            raise ConflictError("行动包尚未转交")
        self._require_receiver(principal, handover)
        if package["status"] != "交接中" or self._handover_state(handover["id"]) not in {"dispatched", "item_received"}:
            raise ConflictError("当前交接环节不能退回")
        receipts = self._receipts_by_key(handover["id"])
        if not any(row["decision"] == "rejected" for row in receipts.values()):
            raise ValidationError("整包退回前必须先逐项退回至少一份材料")
        now = to_storage(self.clock.now())
        self.connection.execute(
            "UPDATE handover_packages SET status='修订中',updated_at=? WHERE id=?", (now, package_id)
        )
        self._event(package_id, handover["id"], "package_returned", principal, now, {"reason": reason})
        self.audit.record(
            self._actor(principal),
            action="handover.return",
            resource_type="handover_package",
            resource_id=package_id,
            after={"handover_id": handover["id"], "reason": reason},
        )
        return self.detail(principal, package_id)

    def reassign(self, principal: Principal, package_id: int, to_department_id: int, reason: str, deadline_hours: int) -> dict:
        principal.require("handover.duty")
        package = self._require_package(package_id)
        handover = self._current_handover(package)
        if handover is None:
            raise ConflictError("行动包尚未转交，不能接管")
        if package["status"] != "交接中":
            raise ConflictError("仅交接中的行动包可以超时接管")
        from app.core.clock import from_storage

        now_dt = self.clock.now()
        deadline = from_storage(handover["deadline_at"])
        if deadline is None or now_dt <= deadline:
            raise ConflictError("交接尚未超过接收期限，不能值班接管")
        if to_department_id == handover["to_department_id"]:
            raise ValidationError("接管改派的部门不能与当前接收部门相同")
        self._require_active_department(to_department_id)
        now = to_storage(now_dt)
        new_deadline = to_storage(now_dt + timedelta(hours=deadline_hours))
        cursor = self.connection.execute(
            "INSERT INTO handover_handovers(package_id,sequence_no,from_department_id,to_department_id,"
            "dispatched_by_user_id,dispatched_by_name,deadline_at,dispatched_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                package_id,
                int(handover["sequence_no"]) + 1,
                handover["from_department_id"],
                to_department_id,
                principal.user_id,
                principal.display_name,
                new_deadline,
                now,
            ),
        )
        new_handover_id = int(cursor.lastrowid)
        self._event(
            package_id,
            handover["id"],
            "taken_over",
            principal,
            now,
            {"reason": reason, "new_handover_id": new_handover_id, "new_to_department_id": to_department_id},
        )
        self.connection.execute(
            "UPDATE handover_packages SET current_handover_id=?,updated_at=? WHERE id=?",
            (new_handover_id, now, package_id),
        )
        self._event(package_id, new_handover_id, "dispatched", principal, now, {"deadline_at": new_deadline, "reason": reason, "taken_over_from": handover["id"]})
        version = self._append_version(self._require_package(package_id), "dispatched", principal.display_name, now, handover_id=new_handover_id)
        self.audit.record(
            self._actor(principal),
            action="handover.reassign",
            resource_type="handover_package",
            resource_id=package_id,
            after={
                "previous_handover_id": handover["id"],
                "new_handover_id": new_handover_id,
                "to_department_id": to_department_id,
                "reason": reason,
                "version_no": version["version_no"],
            },
        )
        return self._serialize_package(self._require_package(package_id), principal)

    # ------------------------------------------------------------------ 留痕更正

    def correct(self, principal: Principal, package_id: int, reason: str, materials: list[dict]) -> dict:
        package = self._require_package(package_id)
        self._require_owner(principal, package)
        if package["status"] == "草稿":
            raise ConflictError("草稿状态请直接编辑材料，无需留痕更正")
        if package["status"] == "交接中":
            raise ConflictError("交接进行中不能更正材料，请先由接收方退回修订")
        if not materials:
            raise ValidationError("更正至少包含一份材料")
        now = to_storage(self.clock.now())
        changed: list[str] = []
        for item in materials:
            key = item["item_key"]
            existing = self.connection.execute(
                "SELECT id FROM handover_materials WHERE package_id=? AND item_key=?", (package_id, key)
            ).fetchone()
            if existing is None:
                self._insert_material(package_id, item, now)
                changed.append(key)
                continue
            self.connection.execute(
                "UPDATE handover_materials SET title=?,material_type=?,content_json=?,is_sensitive=?,"
                "sensitive_fields_json=?,updated_at=? WHERE package_id=? AND item_key=?",
                (
                    item["title"].strip(),
                    item["material_type"],
                    json.dumps(item.get("content", {}), ensure_ascii=False, sort_keys=True),
                    1 if item.get("sensitive", False) else 0,
                    json.dumps(item.get("sensitive_fields", []), ensure_ascii=False),
                    now,
                    package_id,
                    key,
                ),
            )
            changed.append(key)
        version = self._append_version(self._require_package(package_id), "corrected", principal.display_name, now)
        cursor = self.connection.execute(
            "INSERT INTO handover_corrections(package_id,version_no,reason,changed_item_keys_json,"
            "actor_user_id,actor_name,created_at) VALUES(?,?,?,?,?,?,?)",
            (
                package_id,
                version["version_no"],
                reason.strip(),
                json.dumps(changed, ensure_ascii=False),
                principal.user_id,
                principal.display_name,
                now,
            ),
        )
        self.audit.record(
            self._actor(principal),
            action="handover.correct",
            resource_type="handover_package",
            resource_id=package_id,
            before={"reason": reason.strip()},
            after={"version_no": version["version_no"], "correction_id": cursor.lastrowid, "changed_item_keys": changed},
        )
        return self.detail(principal, package_id)

    # ------------------------------------------------------------------ 版本与审计查询

    def list_versions(self, principal: Principal, package_id: int) -> dict:
        self._require_package(package_id)
        self._require_participant(principal, "handover.read", package_id)
        rows = self.connection.execute(
            "SELECT id,version_no,kind,handover_id,prev_digest,digest,created_by_name,created_at"
            " FROM handover_versions WHERE package_id=? ORDER BY version_no",
            (package_id,),
        ).fetchall()
        versions = [dict(row) for row in rows]
        expected = ""
        chain_valid = True
        for version in versions:
            if version["prev_digest"] != expected:
                chain_valid = False
                break
            expected = version["digest"]
        return {"package_id": package_id, "chain_valid": chain_valid, "versions": versions}

    def get_version(self, principal: Principal, package_id: int, version_no: int) -> dict:
        package = self._require_package(package_id)
        self._require_participant(principal, "handover.read", package_id)
        row = self.connection.execute(
            "SELECT * FROM handover_versions WHERE package_id=? AND version_no=?", (package_id, version_no)
        ).fetchone()
        if row is None:
            raise NotFoundError("版本不存在")
        result = dict(row)
        payload = json.loads(result.pop("payload_json"))
        result["payload"] = self._mask_snapshot(payload, self._can_view_sensitive(principal, package))
        return result

    def timeline(self, principal: Principal, package_id: int) -> dict:
        package = self._require_package(package_id)
        self._require_participant(principal, "handover.read", package_id)
        handovers = self.connection.execute(
            "SELECT * FROM handover_handovers WHERE package_id=? ORDER BY sequence_no", (package_id,)
        ).fetchall()
        chain = []
        for handover in handovers:
            item = dict(handover)
            item["state"] = self._handover_state(item["id"])
            item["receipts"] = [
                dict(row)
                for row in self.connection.execute(
                    "SELECT item_key,decision,comment,receiver_name,is_overdue,received_at"
                    " FROM handover_receipts WHERE handover_id=? ORDER BY id",
                    (item["id"],),
                ).fetchall()
            ]
            item["events"] = [
                dict(row)
                for row in self.connection.execute(
                    "SELECT id,event_type,actor_name,payload_json,created_at FROM handover_events WHERE handover_id=? ORDER BY id",
                    (item["id"],),
                ).fetchall()
            ]
            chain.append(item)
        corrections = [
            dict(row)
            for row in self.connection.execute(
                "SELECT version_no,reason,changed_item_keys_json,actor_name,created_at"
                " FROM handover_corrections WHERE package_id=? ORDER BY id",
                (package_id,),
            ).fetchall()
        ]
        return {"package_id": package_id, "status": package["status"], "chain": chain, "corrections": corrections}
