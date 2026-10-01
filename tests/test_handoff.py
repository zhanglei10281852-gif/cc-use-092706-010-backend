from __future__ import annotations

from datetime import timedelta

from app.core.clock import to_storage, utc_now


def _materials() -> list[dict]:
    return [
        {
            "key": "photo-1",
            "kind": "photo_caption",
            "title": "滩涂异常现场照片说明",
            "content": "北岸排水口附近褐色泡沫带，长约 30 米",
            "sensitive": True,
            "sensitive_fields": ["gps", "拍摄人"],
        },
        {
            "key": "metric-1",
            "kind": "measurement",
            "title": "水样检测数值",
            "content": "COD 42mg/L，溶解氧 3.1mg/L",
            "sensitive_fields": ["检测编号"],
        },
        {
            "key": "opinion-1",
            "kind": "disposal_opinion",
            "title": "初步处置意见",
            "content": "建议围挡并复测",
        },
    ]


def _login(client, username: str, password: str = "Worker!23456") -> dict:
    response = client.post(
        "/api/auth/login",
        json={"username": username, "password": password, "client_label": "tests"},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['token']}"}


def _bootstrap_org(client, admin: dict) -> dict:
    """创建园林、水务、志愿者三个部门及各自经办账号，外加值班调度员。"""
    role = client.post(
        "/api/roles",
        headers=admin["headers"],
        json={
            "code": "handoff.worker",
            "name": "行动包经办员",
            "permission_codes": ["handoff.read", "handoff.write"],
        },
    )
    assert role.status_code == 201, role.text
    departments = {}
    for code, name in [("garden", "园林部门"), ("water", "水务部门"), ("volunteer", "志愿者团队")]:
        response = client.post(
            "/api/departments",
            headers=admin["headers"],
            json={"name": name, "manager": f"{code}负责人", "phone": "0571-88888888"},
        )
        assert response.status_code == 201, response.text
        departments[code] = response.json()["id"]
    users = {}
    for code, dep in departments.items():
        response = client.post(
            "/api/users",
            headers=admin["headers"],
            json={
                "username": f"user.{code}",
                "password": "Worker!23456",
                "display_name": f"{code}经办",
                "department_id": dep,
                "role_codes": ["handoff.worker"],
            },
        )
        assert response.status_code == 201, response.text
        users[code] = _login(client, f"user.{code}")
    # 只读账号（无 handoff.write）
    response = client.post(
        "/api/roles",
        headers=admin["headers"],
        json={"code": "handoff.viewer", "name": "行动包查看员", "permission_codes": ["handoff.read"]},
    )
    assert response.status_code == 201, response.text
    response = client.post(
        "/api/users",
        headers=admin["headers"],
        json={
            "username": "user.viewer",
            "password": "Worker!23456",
            "display_name": "只读人员",
            "department_id": departments["volunteer"],
            "role_codes": ["handoff.viewer"],
        },
    )
    assert response.status_code == 201, response.text
    users["viewer"] = _login(client, "user.viewer")
    # 值班调度员：系统角色 duty_officer（handoff.read + handoff.dispatch）
    response = client.post(
        "/api/users",
        headers=admin["headers"],
        json={
            "username": "user.duty",
            "password": "Worker!23456",
            "display_name": "值班员",
            "role_codes": ["duty_officer"],
        },
    )
    assert response.status_code == 201, response.text
    users["duty"] = _login(client, "user.duty")
    return {"departments": departments, "users": users}


def _create_package(client, headers: dict, org: dict, receiver: str = "water") -> dict:
    response = client.post(
        "/api/handoff/packages",
        headers=headers,
        json={
            "title": "北岸滩涂异常联合处置",
            "subject": "排水口泡沫带异常",
            "receiver_department_id": org["departments"][receiver],
            "deadline_hours": 24,
            "materials": _materials(),
            "remark": "请共同核对",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


# ---------------------------------------------------------------- 基本发起

def test_create_package_seals_first_version_and_chain(client, admin):
    org = _bootstrap_org(client, admin)
    package = _create_package(client, org["users"]["garden"], org)
    assert package["status"] == "in_progress"
    assert package["current_version_no"] == 1
    assert len(package["materials"]) == 3
    chain = package["handoff_chain"]
    assert len(chain) == 1
    assert chain[0]["status"] == "pending"
    assert chain[0]["receiver_department_id"] == org["departments"]["water"]
    assert package["versions"][0]["manifest_digest"]
    assert not package["is_overdue"]


def test_create_requires_authentication_and_permission(client, admin):
    org = _bootstrap_org(client, admin)
    payload = {
        "title": "x",
        "subject": "y",
        "receiver_department_id": org["departments"]["water"],
        "deadline_hours": 24,
        "materials": _materials()[:1],
    }
    assert client.post("/api/handoff/packages", json=payload).status_code == 401
    denied = client.post(
        "/api/handoff/packages", headers=org["users"]["viewer"], json=payload
    )
    assert denied.status_code == 403
    same_dep = client.post(
        "/api/handoff/packages",
        headers=org["users"]["garden"],
        json={**payload, "receiver_department_id": org["departments"]["garden"]},
    )
    assert same_dep.status_code == 422


# ---------------------------------------------------------------- 部分接收

def test_partial_acknowledgment_then_full_acceptance_and_forward(client, admin):
    org = _bootstrap_org(client, admin)
    package = _create_package(client, org["users"]["garden"], org)
    pid = package["id"]
    water = org["users"]["water"]

    first = client.post(
        f"/api/handoff/packages/{pid}/acknowledgments",
        headers=water,
        json={"entries": [{"material_key": "photo-1", "accepted": True, "note": "照片清晰"}]},
    )
    assert first.status_code == 201, first.text
    chain = first.json()["handoff_chain"][0]
    assert chain["status"] == "partial"
    assert len(chain["item_acks"]) == 1
    assert chain["is_overdue"] is False

    second = client.post(
        f"/api/handoff/packages/{pid}/acknowledgments",
        headers=water,
        json={"entries": [
            {"material_key": "metric-1", "accepted": True},
            {"material_key": "opinion-1", "accepted": True},
        ]},
    )
    assert second.status_code == 201, second.text
    chain = second.json()["handoff_chain"][0]
    assert chain["status"] == "accepted"
    assert chain["acknowledged_at"]
    assert {item["material_key"] for item in chain["item_acks"]} == {
        "photo-1",
        "metric-1",
        "opinion-1",
    }

    forward = client.post(
        f"/api/handoff/packages/{pid}/forward",
        headers=water,
        json={
            "next_department_id": org["departments"]["volunteer"],
            "deadline_hours": 12,
            "note": "材料齐全，请志愿者团队安排巡护",
        },
    )
    assert forward.status_code == 200, forward.text
    forwarded = forward.json()
    assert len(forwarded["handoff_chain"]) == 2
    assert forwarded["handoff_chain"][0]["status"] == "forwarded"
    assert forwarded["handoff_chain"][1]["status"] == "pending"
    assert forwarded["handoff_chain"][1]["receiver_department_id"] == org["departments"]["volunteer"]


def test_duplicate_acknowledgment_is_rejected(client, admin):
    org = _bootstrap_org(client, admin)
    package = _create_package(client, org["users"]["garden"], org)
    pid = package["id"]
    water = org["users"]["water"]
    body = {"entries": [{"material_key": "photo-1", "accepted": True}]}
    assert client.post(f"/api/handoff/packages/{pid}/acknowledgments", headers=water, json=body).status_code == 201
    duplicate = client.post(f"/api/handoff/packages/{pid}/acknowledgments", headers=water, json=body)
    assert duplicate.status_code == 409
    unknown = client.post(
        f"/api/handoff/packages/{pid}/acknowledgments",
        headers=water,
        json={"entries": [{"material_key": "not-exists", "accepted": True}]},
    )
    assert unknown.status_code == 422


# ---------------------------------------------------------------- 退回修订

def test_return_revision_creates_non_overwritable_version(client, admin):
    from app.database import get_connection
    from app.handoff.service import manifest_digest

    org = _bootstrap_org(client, admin)
    package = _create_package(client, org["users"]["garden"], org)
    pid = package["id"]
    original_digest = package["versions"][0]["manifest_digest"]

    returned = client.post(
        f"/api/handoff/packages/{pid}/return",
        headers=org["users"]["water"],
        json={"reason": "检测数值缺检测编号，请补充后重交"},
    )
    assert returned.status_code == 200, returned.text
    assert returned.json()["handoff_chain"][0]["status"] == "returned"

    # 非发起方不能修订
    forbidden = client.post(
        f"/api/handoff/packages/{pid}/revisions",
        headers=org["users"]["water"],
        json={"deadline_hours": 24, "materials": _materials()},
    )
    assert forbidden.status_code == 403

    revised_materials = _materials()
    revised_materials[1]["content"] = "COD 42mg/L，溶解氧 3.1mg/L，检测编号 W-2026-0901"
    revised = client.post(
        f"/api/handoff/packages/{pid}/revisions",
        headers=org["users"]["garden"],
        json={"deadline_hours": 48, "materials": revised_materials, "remark": "已补检测编号"},
    )
    assert revised.status_code == 201, revised.text
    body = revised.json()
    assert body["current_version_no"] == 2
    assert len(body["handoff_chain"]) == 2
    assert body["handoff_chain"][1]["status"] == "pending"

    # 旧版本仍可读取且内容、摘要未被覆盖
    v1 = client.get(f"/api/handoff/packages/{pid}/versions/1", headers=org["users"]["water"])
    assert v1.status_code == 200
    assert "W-2026-0901" not in v1.text
    assert v1.json()["manifest_digest"] == original_digest
    assert v1.json()["digest_verified"] is True
    v2 = client.get(f"/api/handoff/packages/{pid}/versions/2", headers=org["users"]["water"])
    assert v2.json()["manifest_digest"] != original_digest
    assert v2.json()["digest_verified"] is True

    # 绕过接口直接改库：摘要校验立即暴露篡改
    connection = get_connection()
    connection.execute(
        "UPDATE handoff_versions SET materials_json=? WHERE package_id=? AND version_no=1",
        ('[{"key":"photo-1","kind":"photo_caption","title":"被篡改","content":"x","sensitive":false,"sensitive_fields":[]}]', pid),
    )
    tampered = client.get(f"/api/handoff/packages/{pid}/versions/1", headers=org["users"]["water"])
    assert tampered.json()["digest_verified"] is False
    detail = client.get(f"/api/handoff/packages/{pid}", headers=org["users"]["water"])
    assert detail.json()["versions"][0]["digest_verified"] is False
    connection.execute(
        "UPDATE handoff_versions SET materials_json=(SELECT materials_json FROM handoff_versions WHERE package_id=? AND version_no=2) WHERE package_id=? AND version_no=1",
        (pid, pid),
    )
    assert v2.json()["manifest_digest"] == manifest_digest(
        [
            {
                "key": m["key"],
                "kind": m["kind"],
                "title": m["title"],
                "content": m["content"],
                "sensitive": bool(m.get("sensitive")) or bool(m.get("sensitive_fields")),
                "sensitive_fields": m.get("sensitive_fields", []),
            }
            for m in revised_materials
        ]
    )

    # 数据库层兜底：同包同版本号不能再次写入
    connection = get_connection()
    import sqlite3

    with __import__("pytest").raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO handoff_versions(package_id,version_no,materials_json,manifest_digest,"
            "created_by_name,created_at) VALUES(?,?,?,?,?,?)",
            (pid, 1, "[]", "x", "attacker", to_storage(utc_now())),
        )


def test_rejected_item_marks_handoff_returned(client, admin):
    org = _bootstrap_org(client, admin)
    package = _create_package(client, org["users"]["garden"], org)
    pid = package["id"]
    water = org["users"]["water"]
    client.post(
        f"/api/handoff/packages/{pid}/acknowledgments",
        headers=water,
        json={"entries": [{"material_key": "photo-1", "accepted": True}]},
    )
    final = client.post(
        f"/api/handoff/packages/{pid}/acknowledgments",
        headers=water,
        json={"entries": [
            {"material_key": "metric-1", "accepted": False, "note": "数值存疑"},
            {"material_key": "opinion-1", "accepted": True},
        ]},
    )
    assert final.status_code == 201
    assert final.json()["handoff_chain"][0]["status"] == "returned"
    # 已退回状态不能再确认
    again = client.post(
        f"/api/handoff/packages/{pid}/acknowledgments",
        headers=water,
        json={"entries": [{"material_key": "metric-1", "accepted": True}]},
    )
    assert again.status_code == 409


# ---------------------------------------------------------------- 超时接管

def _backdate_deadline(package_id: int) -> None:
    from app.database import get_connection

    connection = get_connection()
    connection.execute(
        "UPDATE handoff_records SET deadline_at=? WHERE package_id=? "
        "AND seq=(SELECT MAX(seq) FROM handoff_records WHERE package_id=?)",
        (to_storage(utc_now() - timedelta(minutes=1)), package_id, package_id),
    )


def test_overdue_reassignment_by_duty_officer(client, admin):
    org = _bootstrap_org(client, admin)
    package = _create_package(client, org["users"]["garden"], org)
    pid = package["id"]

    # 未超期不能接管
    early = client.post(
        f"/api/handoff/packages/{pid}/reassign",
        headers=org["users"]["duty"],
        json={
            "new_receiver_department_id": org["departments"]["volunteer"],
            "deadline_hours": 12,
            "reason": "测试提前接管",
        },
    )
    assert early.status_code == 409

    # 普通经办没有接管权限
    _backdate_deadline(pid)
    overdue = client.get("/api/handoff/packages/overdue", headers=org["users"]["duty"])
    assert overdue.status_code == 200
    assert any(item["package_id"] == pid and item["is_overdue"] for item in overdue.json()["data"])

    forbidden = client.post(
        f"/api/handoff/packages/{pid}/reassign",
        headers=org["users"]["garden"],
        json={
            "new_receiver_department_id": org["departments"]["volunteer"],
            "deadline_hours": 12,
            "reason": "无权接管",
        },
    )
    assert forbidden.status_code == 403

    reassigned = client.post(
        f"/api/handoff/packages/{pid}/reassign",
        headers=org["users"]["duty"],
        json={
            "new_receiver_department_id": org["departments"]["volunteer"],
            "deadline_hours": 12,
            "reason": "水务逾期未确认，改派志愿者团队",
        },
    )
    assert reassigned.status_code == 200, reassigned.text
    chain = reassigned.json()["handoff_chain"]
    assert chain[0]["status"] == "reassigned"
    assert chain[0]["dispatched_by_name"] == "值班员"
    assert chain[1]["status"] == "pending"
    assert chain[1]["receiver_department_id"] == org["departments"]["volunteer"]
    assert not chain[1]["is_overdue"]

    # 新接收方可以逐项确认
    ack = client.post(
        f"/api/handoff/packages/{pid}/acknowledgments",
        headers=org["users"]["volunteer"],
        json={"entries": [{"material_key": m["key"], "accepted": True} for m in _materials()]},
    )
    assert ack.status_code == 201, ack.text
    assert ack.json()["handoff_chain"][-1]["status"] == "accepted"


# ---------------------------------------------------------------- 权限隔离

def test_permission_isolation_between_departments(client, admin):
    org = _bootstrap_org(client, admin)
    package = _create_package(client, org["users"]["garden"], org, receiver="water")
    pid = package["id"]

    # 志愿者团队既看不到，也不能确认、退回
    assert client.get(f"/api/handoff/packages/{pid}", headers=org["users"]["volunteer"]).status_code == 403
    ack = client.post(
        f"/api/handoff/packages/{pid}/acknowledgments",
        headers=org["users"]["volunteer"],
        json={"entries": [{"material_key": "photo-1", "accepted": True}]},
    )
    assert ack.status_code == 403
    back = client.post(
        f"/api/handoff/packages/{pid}/return",
        headers=org["users"]["volunteer"],
        json={"reason": "无权"},
    )
    assert back.status_code == 403

    # 只读账号可见本部门相关数据但不能操作（此包与其部门无关，故不可见）
    assert client.get(f"/api/handoff/packages/{pid}", headers=org["users"]["viewer"]).status_code == 403

    # 列表只能看到本部门参与的包
    listing = client.get("/api/handoff/packages", headers=org["users"]["volunteer"])
    assert listing.status_code == 200
    assert listing.json()["total"] == 0
    water_listing = client.get("/api/handoff/packages", headers=org["users"]["water"])
    assert water_listing.json()["total"] == 1

    # 值班员跨部门可见，但没有 handoff.write 不能确认
    duty_view = client.get(f"/api/handoff/packages/{pid}", headers=org["users"]["duty"])
    assert duty_view.status_code == 200
    ack_as_duty = client.post(
        f"/api/handoff/packages/{pid}/acknowledgments",
        headers=org["users"]["duty"],
        json={"entries": [{"material_key": "photo-1", "accepted": True}]},
    )
    assert ack_as_duty.status_code == 403


# ---------------------------------------------------------------- 完成封存

def test_completed_package_cannot_be_silently_modified(client, admin):
    org = _bootstrap_org(client, admin)
    package = _create_package(client, org["users"]["garden"], org)
    pid = package["id"]
    water = org["users"]["water"]
    client.post(
        f"/api/handoff/packages/{pid}/acknowledgments",
        headers=water,
        json={"entries": [{"material_key": m["key"], "accepted": True} for m in _materials()]},
    )
    completed = client.post(
        f"/api/handoff/packages/{pid}/complete",
        headers=water,
        json={"conclusion": "三方已现场复核，围挡完成，结案"},
    )
    assert completed.status_code == 200, completed.text
    assert completed.json()["locked"] is True
    assert completed.json()["status"] == "completed"

    ack_body = {"entries": [{"material_key": "photo-1", "accepted": False}]}
    assert client.post(f"/api/handoff/packages/{pid}/acknowledgments", headers=water, json=ack_body).status_code == 409
    assert client.post(f"/api/handoff/packages/{pid}/return", headers=water, json={"reason": "x"}).status_code == 409
    assert client.post(
        f"/api/handoff/packages/{pid}/revisions",
        headers=org["users"]["garden"],
        json={"deadline_hours": 1, "materials": _materials()},
    ).status_code == 409
    assert client.post(
        f"/api/handoff/packages/{pid}/forward",
        headers=water,
        json={"next_department_id": org["departments"]["volunteer"], "deadline_hours": 1},
    ).status_code == 409
    _backdate_deadline(pid)
    assert client.post(
        f"/api/handoff/packages/{pid}/reassign",
        headers=org["users"]["duty"],
        json={
            "new_receiver_department_id": org["departments"]["volunteer"],
            "deadline_hours": 1,
            "reason": "已结案不应改派",
        },
    ).status_code == 409

    # 封存拒绝被审计记录
    events = client.get(
        "/api/audit",
        headers=admin["headers"],
        params={"resource_type": "handoff_package", "action": "handoff.locked.rejected", "outcome": "denied"},
    )
    assert events.status_code == 200
    assert events.json()["total"] >= 1

    # 完成后仍可只读查看，交接链与版本保留
    detail = client.get(f"/api/handoff/packages/{pid}", headers=water)
    assert detail.status_code == 200
    assert detail.json()["handoff_chain"][-1]["status"] == "completed"


def test_complete_requires_full_acceptance(client, admin):
    org = _bootstrap_org(client, admin)
    package = _create_package(client, org["users"]["garden"], org)
    pid = package["id"]
    water = org["users"]["water"]
    client.post(
        f"/api/handoff/packages/{pid}/acknowledgments",
        headers=water,
        json={"entries": [{"material_key": "photo-1", "accepted": True}]},
    )
    blocked = client.post(
        f"/api/handoff/packages/{pid}/complete",
        headers=water,
        json={"conclusion": "尚未收齐"},
    )
    assert blocked.status_code == 409


# ---------------------------------------------------------------- 审计查询

def test_audit_trail_covers_full_handoff_lifecycle(client, admin):
    org = _bootstrap_org(client, admin)
    package = _create_package(client, org["users"]["garden"], org)
    pid = package["id"]
    client.post(
        f"/api/handoff/packages/{pid}/return",
        headers=org["users"]["water"],
        json={"reason": "补充材料"},
    )
    client.post(
        f"/api/handoff/packages/{pid}/revisions",
        headers=org["users"]["garden"],
        json={"deadline_hours": 24, "materials": _materials()},
    )
    client.post(
        f"/api/handoff/packages/{pid}/acknowledgments",
        headers=org["users"]["water"],
        json={"entries": [{"material_key": m["key"], "accepted": True} for m in _materials()]},
    )
    _backdate_deadline(pid)

    chain = client.get(f"/api/handoff/packages/{pid}/chain", headers=org["users"]["garden"])
    assert chain.status_code == 200
    assert [item["seq"] for item in chain.json()["data"]] == [1, 2]

    events = client.get(
        "/api/audit",
        headers=admin["headers"],
        params={"resource_type": "handoff_package"},
    )
    assert events.status_code == 200
    actions = {event["action"] for event in events.json()["data"]}
    assert {
        "handoff.package.create",
        "handoff.package.return",
        "handoff.package.revise",
        "handoff.items.ack",
    }.issubset(actions)

    # 无审计权限的经办账号不能查询审计
    assert client.get("/api/audit", headers=org["users"]["water"]).status_code == 403
