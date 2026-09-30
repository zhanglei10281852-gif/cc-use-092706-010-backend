from __future__ import annotations

import sqlite3

import pytest

API = "/api/handover/packages"
PASSWORD = "Handover!234"


def _login(client, username: str) -> dict:
    response = client.post(
        "/api/auth/login",
        json={"username": username, "password": PASSWORD, "client_label": "tests"},
    )
    assert response.status_code == 200, response.text
    token = response.json()["token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def world(client, admin):
    def make_department(name: str) -> int:
        response = client.post(
            "/api/departments",
            headers=admin["headers"],
            json={"name": name, "manager": f"{name}负责人", "phone": "0571-88888888"},
        )
        assert response.status_code == 201, response.text
        return response.json()["id"]

    def make_role(code: str, permissions: list[str]) -> None:
        response = client.post(
            "/api/roles",
            headers=admin["headers"],
            json={"code": code, "name": code, "permission_codes": permissions},
        )
        assert response.status_code == 201, response.text

    def make_user(username: str, display_name: str, department_id: int | None, role: str) -> dict:
        response = client.post(
            "/api/users",
            headers=admin["headers"],
            json={
                "username": username,
                "password": PASSWORD,
                "display_name": display_name,
                "department_id": department_id,
                "role_codes": [role],
            },
        )
        assert response.status_code == 201, response.text
        return _login(client, username)

    make_role("pkg_staff", ["handover.read", "handover.manage"])
    make_role("pkg_duty", ["handover.read", "handover.duty"])
    make_role("pkg_noaccess", ["residents.read"])

    dept_a = make_department("园林科")
    dept_b = make_department("水务科")
    dept_c = make_department("志愿者协调办")
    dept_d = make_department("值班室")

    return {
        "client": client,
        "admin": admin["headers"],
        "departments": {"a": dept_a, "b": dept_b, "c": dept_c, "d": dept_d},
        "users": {
            "a": make_user("gardener.a", "园林经办人", dept_a, "pkg_staff"),
            "b": make_user("water.b", "水务经办人", dept_b, "pkg_staff"),
            "c": make_user("volunteer.c", "志愿者经办人", dept_c, "pkg_staff"),
            "duty": make_user("duty.d", "值班长", dept_d, "pkg_duty"),
            "outsider": make_user("outsider.x", "无关人员", dept_c, "pkg_noaccess"),
        },
    }


def package_payload() -> dict:
    return {
        "title": "三号滩涂异常联合处置",
        "description": "疑似污水汇入，需跨部门核对材料",
        "incident_ref": "TAN-2026-003",
        "materials": [
            {
                "item_key": "photo-caption",
                "title": "现场照片说明",
                "material_type": "photo_caption",
                "content": {"caption": "退潮后可见深色水带", "photo_id": "IMG-0032", "shooter_phone": "13900000000"},
                "sensitive": True,
                "sensitive_fields": ["shooter_phone"],
            },
            {
                "item_key": "measurement",
                "title": "水质检测数值",
                "material_type": "measurement",
                "content": {"ph": 6.1, "cod_mg_l": 42.7, "sampler_id_card": "330100199001011234"},
                "sensitive": True,
                "sensitive_fields": ["sampler_id_card"],
            },
            {
                "item_key": "opinion",
                "title": "初步处置意见",
                "material_type": "opinion",
                "content": {"proposal": "截流取样复核"},
                "sensitive": False,
                "sensitive_fields": [],
            },
        ],
    }


def create_package(world) -> int:
    response = world["client"].post(API, headers=world["users"]["a"], json=package_payload())
    assert response.status_code == 201, response.text
    package_id = response.json()["id"]
    assert response.json()["status"] == "草稿"
    assert response.json()["receipt_summary"] == {"total": 3, "accepted": 0, "rejected": 0, "pending": 3}
    return package_id


def dispatch(world, package_id: int, target: str = "b", deadline_hours: int = 48) -> dict:
    response = world["client"].post(
        f"{API}/{package_id}/dispatch",
        headers=world["users"]["a"],
        json={"to_department_id": world["departments"][target], "deadline_hours": deadline_hours},
    )
    assert response.status_code == 200, response.text
    return response.json()


# --------------------------------------------------------------------- 部分接收


def test_partial_receipt_keeps_package_in_flight(world):
    client = world["client"]
    package_id = create_package(world)
    body = dispatch(world, package_id)
    handover_id = body["current_handover"]["id"]
    assert body["current_handover"]["sequence_no"] == 1
    assert body["current_handover"]["is_overdue"] is False

    response = client.post(
        f"{API}/{package_id}/receipts",
        headers=world["users"]["b"],
        json={"decisions": [{"item_key": "photo-caption", "decision": "accepted", "comment": "照片清晰"}]},
    )
    assert response.status_code == 200, response.text
    assert response.json()["receipt_summary"] == {"total": 3, "accepted": 1, "rejected": 0, "pending": 2}
    assert response.json()["status"] == "交接中"

    response = client.post(
        f"{API}/{package_id}/receipts",
        headers=world["users"]["b"],
        json={"decisions": [{"item_key": "measurement", "decision": "rejected", "comment": "缺少采样点位"}]},
    )
    assert response.status_code == 200, response.text
    assert response.json()["receipt_summary"] == {"total": 3, "accepted": 1, "rejected": 1, "pending": 1}

    photo = next(item for item in response.json()["materials"] if item["item_key"] == "photo-caption")
    assert photo["receipt"]["decision"] == "accepted"
    assert photo["receipt"]["receiver_name"] == "水务经办人"
    assert photo["receipt"]["is_overdue"] is False
    assert handover_id > 0


def test_full_acceptance_completes_package(world):
    client = world["client"]
    package_id = create_package(world)
    dispatch(world, package_id)
    for key in ("photo-caption", "measurement", "opinion"):
        response = client.post(
            f"{API}/{package_id}/receipts",
            headers=world["users"]["b"],
            json={"decisions": [{"item_key": key, "decision": "accepted"}]},
        )
        assert response.status_code == 200, response.text
    assert response.json()["status"] == "已完成"
    assert response.json()["archived_at"] is not None


# --------------------------------------------------------------------- 退回修订


def test_return_requires_rejected_item(world):
    client = world["client"]
    package_id = create_package(world)
    dispatch(world, package_id)
    response = client.post(
        f"{API}/{package_id}/return",
        headers=world["users"]["b"],
        json={"reason": "材料看不懂"},
    )
    assert response.status_code == 422
    assert "逐项退回" in response.json()["error"]["message"]


def test_return_revise_and_redispatch_resets_confirmations(world):
    client = world["client"]
    package_id = create_package(world)
    dispatch(world, package_id)

    client.post(
        f"{API}/{package_id}/receipts",
        headers=world["users"]["b"],
        json={"decisions": [
            {"item_key": "photo-caption", "decision": "accepted"},
            {"item_key": "measurement", "decision": "rejected", "comment": "缺采样点位"},
        ]},
    )
    returned = client.post(
        f"{API}/{package_id}/return",
        headers=world["users"]["b"],
        json={"reason": "检测数值缺少采样点位，请补充后重新转交"},
    )
    assert returned.status_code == 200, returned.text
    assert returned.json()["status"] == "修订中"

    # 交接中不能直接改材料；修订中由发起部门修改
    patched = client.patch(
        f"{API}/{package_id}/materials/measurement",
        headers=world["users"]["a"],
        json={"content": {"ph": 6.1, "cod_mg_l": 42.7, "site": "三号滩涂东侧 50m", "sampler_id_card": "330100199001011234"}},
    )
    assert patched.status_code == 200, patched.text

    redispatched = dispatch(world, package_id)
    current = redispatched["current_handover"]
    assert current["sequence_no"] == 2
    # 新一轮交接不能沿用旧的已确认状态
    assert redispatched["receipt_summary"] == {"total": 3, "accepted": 0, "rejected": 0, "pending": 3}

    timeline = client.get(f"{API}/{package_id}/timeline", headers=world["users"]["a"])
    assert timeline.status_code == 200
    chain = timeline.json()["chain"]
    assert [item["sequence_no"] for item in chain] == [1, 2]
    assert chain[0]["state"] == "package_returned"
    assert len(chain[0]["receipts"]) == 2
    assert chain[1]["receipts"] == []


# --------------------------------------------------------------------- 重复确认


def test_duplicate_confirmation_is_rejected(world):
    client = world["client"]
    package_id = create_package(world)
    dispatch(world, package_id)
    payload = {"decisions": [{"item_key": "opinion", "decision": "accepted"}]}
    first = client.post(f"{API}/{package_id}/receipts", headers=world["users"]["b"], json=payload)
    assert first.status_code == 200

    # 同一材料再次确认 -> 409，原确认不被覆盖
    second = client.post(f"{API}/{package_id}/receipts", headers=world["users"]["b"], json=payload)
    assert second.status_code == 409
    assert "重复确认" in second.json()["error"]["message"]

    detail = client.get(f"{API}/{package_id}", headers=world["users"]["b"])
    opinion = next(item for item in detail.json()["materials"] if item["item_key"] == "opinion")
    assert opinion["receipt"]["decision"] == "accepted"

    # 同一批次里重复列出材料 -> 422
    batch = client.post(
        f"{API}/{package_id}/receipts",
        headers=world["users"]["b"],
        json={"decisions": [
            {"item_key": "measurement", "decision": "accepted"},
            {"item_key": "measurement", "decision": "rejected"},
        ]},
    )
    assert batch.status_code == 422

    # 不存在的材料标识 -> 404
    missing = client.post(
        f"{API}/{package_id}/receipts",
        headers=world["users"]["b"],
        json={"decisions": [{"item_key": "nope", "decision": "accepted"}]},
    )
    assert missing.status_code == 404


# --------------------------------------------------------------------- 超时接管


def _dispatch_at(world, package_id: int, target: str, when, deadline_hours: int = 48) -> None:
    """用受控时钟发起交接，便于构造已超期的责任环节而不触碰不可变记录。"""
    from app.core.clock import FrozenClock
    from app.core.security import Principal
    from app.database import transaction
    from app.handover.service import HandoverService

    me = world["client"].get("/api/auth/me", headers=world["users"]["a"]).json()
    principal = Principal(
        user_id=me["user_id"],
        username=me["username"],
        display_name=me["display_name"],
        department_id=me["department_id"],
        permissions=frozenset(me["permissions"]),
        session_id=me["session_id"],
    )
    with transaction(immediate=True) as connection:
        HandoverService(connection, FrozenClock(when)).dispatch(
            principal, package_id, world["departments"][target], deadline_hours
        )


def test_duty_reassign_only_after_timeout(world):
    from datetime import UTC, datetime

    client = world["client"]
    package_id = create_package(world)
    _dispatch_at(world, package_id, "b", datetime(2020, 1, 1, tzinfo=UTC))

    # 未超期不能接管（先用受控时钟验证）：同批次构造一个未超期的包
    fresh_id = create_package(world)
    _dispatch_at(world, fresh_id, "b", datetime(2099, 1, 1, tzinfo=UTC))
    early = client.post(
        f"{API}/{fresh_id}/reassign",
        headers=world["users"]["duty"],
        json={"to_department_id": world["departments"]["c"], "reason": "超时未接收", "deadline_hours": 24},
    )
    assert early.status_code == 409

    overdue = client.get(f"{API}/{package_id}", headers=world["users"]["a"])
    assert overdue.json()["current_handover"]["is_overdue"] is True

    # 值班人员能在列表与详情中看到超期包，但敏感内容保持脱敏
    duty_list = client.get(API, headers=world["users"]["duty"])
    assert duty_list.status_code == 200
    assert {item["id"] for item in duty_list.json()["data"]} >= {package_id, fresh_id}
    duty_detail = client.get(f"{API}/{package_id}", headers=world["users"]["duty"])
    assert duty_detail.status_code == 200
    measurement = next(item for item in duty_detail.json()["materials"] if item["item_key"] == "measurement")
    assert measurement["content"]["sampler_id_card"] == "******"

    # 普通经办员不能接管
    forbidden = client.post(
        f"{API}/{package_id}/reassign",
        headers=world["users"]["c"],
        json={"to_department_id": world["departments"]["c"], "reason": "抢单", "deadline_hours": 24},
    )
    assert forbidden.status_code == 403

    taken = client.post(
        f"{API}/{package_id}/reassign",
        headers=world["users"]["duty"],
        json={"to_department_id": world["departments"]["c"], "reason": "水务科超时未确认，改派志愿者团队", "deadline_hours": 24},
    )
    assert taken.status_code == 200, taken.text
    current = taken.json()["current_handover"]
    assert current["sequence_no"] == 2
    assert current["to_department_id"] == world["departments"]["c"]
    assert current["state"] == "dispatched"
    assert current["is_overdue"] is False

    # 原接收方不能再确认旧环节；新接收方可以逐项确认并办结
    stale = client.post(
        f"{API}/{package_id}/receipts",
        headers=world["users"]["b"],
        json={"decisions": [{"item_key": "opinion", "decision": "accepted"}]},
    )
    assert stale.status_code == 403

    for key in ("photo-caption", "measurement", "opinion"):
        response = client.post(
            f"{API}/{package_id}/receipts",
            headers=world["users"]["c"],
            json={"decisions": [{"item_key": key, "decision": "accepted"}]},
        )
        assert response.status_code == 200, response.text
    assert response.json()["status"] == "已完成"


# --------------------------------------------------------------------- 权限隔离


def test_permission_and_department_isolation(world):
    client = world["client"]

    # 未认证
    assert client.get(API).status_code == 401
    # 无行动包权限
    assert client.get(API, headers=world["users"]["outsider"]).status_code == 403

    package_id = create_package(world)

    # 非参与部门看不到行动包详情
    forbidden = client.get(f"{API}/{package_id}", headers=world["users"]["c"])
    assert forbidden.status_code == 403

    dispatch(world, package_id)

    # 接收部门可以看，且只有当前接收方可以确认
    detail = client.get(f"{API}/{package_id}", headers=world["users"]["b"])
    assert detail.status_code == 200
    wrong_side = client.post(
        f"{API}/{package_id}/receipts",
        headers=world["users"]["a"],
        json={"decisions": [{"item_key": "opinion", "decision": "accepted"}]},
    )
    assert wrong_side.status_code == 403

    # 非参与部门仍被拒绝
    assert client.get(f"{API}/{package_id}", headers=world["users"]["c"]).status_code == 403


def test_sensitive_fields_visible_only_to_current_parties(world):
    client = world["client"]
    package_id = create_package(world)

    # 发起部门可见明文
    owner = client.get(f"{API}/{package_id}", headers=world["users"]["a"]).json()
    measurement = next(item for item in owner["materials"] if item["item_key"] == "measurement")
    assert measurement["content"]["sampler_id_card"] == "330100199001011234"

    dispatch(world, package_id)

    # 当前接收部门可见明文
    receiver = client.get(f"{API}/{package_id}", headers=world["users"]["b"]).json()
    measurement = next(item for item in receiver["materials"] if item["item_key"] == "measurement")
    assert measurement["content"]["sampler_id_card"] == "330100199001011234"
    photo = next(item for item in receiver["materials"] if item["item_key"] == "photo-caption")
    assert photo["content"]["shooter_phone"] == "13900000000"

    # 退回后改派给志愿者协调办，原接收方（历史参与方）只能看到掩码
    client.post(
        f"{API}/{package_id}/receipts",
        headers=world["users"]["b"],
        json={"decisions": [{"item_key": "measurement", "decision": "rejected", "comment": "缺点位"}]},
    )
    client.post(f"{API}/{package_id}/return", headers=world["users"]["b"], json={"reason": "补点位"})
    client.patch(
        f"{API}/{package_id}/materials/measurement",
        headers=world["users"]["a"],
        json={"content": {"ph": 6.1, "cod_mg_l": 42.7, "site": "东侧", "sampler_id_card": "330100199001011234"}},
    )
    dispatch(world, package_id, target="c")

    historical = client.get(f"{API}/{package_id}", headers=world["users"]["b"]).json()
    measurement = next(item for item in historical["materials"] if item["item_key"] == "measurement")
    assert measurement["content"]["sampler_id_card"] == "******"
    assert measurement["content"]["ph"] == 6.1  # 非敏感字段仍可见

    current = client.get(f"{API}/{package_id}", headers=world["users"]["c"]).json()
    measurement = next(item for item in current["materials"] if item["item_key"] == "measurement")
    assert measurement["content"]["sampler_id_card"] == "330100199001011234"

    # 历史版本快照同样对历史参与方脱敏
    version = client.get(f"{API}/{package_id}/versions/3", headers=world["users"]["b"])
    assert version.status_code == 200
    snapshot_measurement = next(
        item for item in version.json()["payload"]["materials"] if item["item_key"] == "measurement"
    )
    assert snapshot_measurement["content"]["sampler_id_card"] == "******"


# --------------------------------------------------------------------- 版本不可覆盖 + 完成封存


def test_immutable_versions_and_sealed_package(world):
    client = world["client"]
    package_id = create_package(world)
    dispatch(world, package_id)
    for key in ("photo-caption", "measurement", "opinion"):
        client.post(
            f"{API}/{package_id}/receipts",
            headers=world["users"]["b"],
            json={"decisions": [{"item_key": key, "decision": "accepted"}]},
        )

    versions = client.get(f"{API}/{package_id}/versions", headers=world["users"]["a"])
    assert versions.status_code == 200
    body = versions.json()
    assert body["chain_valid"] is True
    assert [item["kind"] for item in body["versions"]] == ["created", "dispatched", "completed"]
    # 链式摘要：每一环都引用上一环
    for previous, current in zip(body["versions"], body["versions"][1:]):
        assert current["prev_digest"] == previous["digest"]

    # 已完成包禁止无痕修改
    assert client.patch(
        f"{API}/{package_id}", headers=world["users"]["a"], json={"title": "被篡改的标题"}
    ).status_code == 409
    assert client.post(
        f"{API}/{package_id}/materials",
        headers=world["users"]["a"],
        json={"item_key": "new", "title": "新料", "material_type": "note", "content": {}},
    ).status_code == 409
    assert client.delete(
        f"{API}/{package_id}/materials/opinion", headers=world["users"]["a"]
    ).status_code == 409
    assert client.post(
        f"{API}/{package_id}/dispatch",
        headers=world["users"]["a"],
        json={"to_department_id": world["departments"]["c"]},
    ).status_code == 409

    # 允许留痕更正：生成新版本，不破坏链
    correction = client.post(
        f"{API}/{package_id}/corrections",
        headers=world["users"]["a"],
        json={
            "reason": "检测设备校准后修正 COD 数值",
            "materials": [{
                "item_key": "measurement",
                "title": "水质检测数值",
                "material_type": "measurement",
                "content": {"ph": 6.2, "cod_mg_l": 39.5, "sampler_id_card": "330100199001011234"},
                "sensitive": True,
                "sensitive_fields": ["sampler_id_card"],
            }],
        },
    )
    assert correction.status_code == 200, correction.text
    versions_after = client.get(f"{API}/{package_id}/versions", headers=world["users"]["a"]).json()
    assert versions_after["chain_valid"] is True
    assert [item["kind"] for item in versions_after["versions"]] == ["created", "dispatched", "completed", "corrected"]

    # 数据库层面禁止改写/删除版本与责任记录
    from app.database import get_connection

    connection = get_connection()
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute("UPDATE handover_versions SET digest='tampered' WHERE package_id=?", (package_id,))
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute("DELETE FROM handover_versions WHERE package_id=?", (package_id,))
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute("UPDATE handover_handovers SET deadline_at='2099-01-01T00:00:00+00:00' WHERE package_id=?", (package_id,))
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute("DELETE FROM handover_receipts WHERE package_id=?", (package_id,))


# --------------------------------------------------------------------- 审计查询


def test_audit_query_covers_handover_lifecycle(world):
    client = world["client"]
    package_id = create_package(world)
    dispatch(world, package_id)
    client.post(
        f"{API}/{package_id}/receipts",
        headers=world["users"]["b"],
        json={"decisions": [
            {"item_key": "photo-caption", "decision": "accepted"},
            {"item_key": "measurement", "decision": "rejected", "comment": "缺点位"},
        ]},
    )
    client.post(f"{API}/{package_id}/return", headers=world["users"]["b"], json={"reason": "补齐点位"})

    response = client.get(
        "/api/audit?resource_type=handover_package&size=100",
        headers=world["admin"],
    )
    assert response.status_code == 200
    actions = {event["action"] for event in response.json()["data"] if str(event["resource_id"]) == str(package_id)}
    assert {"handover.create", "handover.dispatch", "handover.receive", "handover.return"} <= actions

    # 无审计权限的经办员不能查询审计
    forbidden = client.get("/api/audit?resource_type=handover_package", headers=world["users"]["b"])
    assert forbidden.status_code == 403
