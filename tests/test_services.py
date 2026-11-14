"""services 层关键业务流测试：审批流、叠加视图、上下架请求两阶段审核。"""
import json

from app import services, storage
from app.core import ENTITIES, normalize_row, record_key


def add_official(entity, row):
    """绕过审批直接写正式表（测试铺底数据用）。"""
    records = storage.read_records(entity)
    records.append(normalize_row(entity, row))
    storage.write_records(entity, records)


def get_pending_changes():
    with storage.db() as conn:
        rows = conn.execute("SELECT * FROM changes WHERE status='pending'").fetchall()
    return [dict(row) for row in rows]


class TestApprovalFlow:
    def test_add_and_approve(self, app_env, admin):
        add_official("cabinets", {"机房": "机房A", "机柜": "C1", "机位": "1-10"})

        row = normalize_row("cabinets", {"机房": "机房A", "机柜": "C2", "机位": "1-8"})
        key = record_key(ENTITIES["cabinets"], row)
        services.submit_change("cabinets", "新增", key, row, submitter="admin")

        changes = get_pending_changes()
        assert len(changes) == 1
        assert changes[0]["operation"] == "新增"

        with storage.db() as conn:
            message = services.apply_review_action(conn, changes[0], {"action": "approve"}, reviewer="admin")
        assert "审核通过" in message

        cabinets = {(c["机房"], c["机柜"]) for c in storage.read_records("cabinets")}
        assert ("机房A", "C2") in cabinets

    def test_reject_leaves_official_untouched(self, app_env, admin):
        row = normalize_row("cabinets", {"机房": "机房A", "机柜": "C9", "机位": "1-8"})
        key = record_key(ENTITIES["cabinets"], row)
        services.submit_change("cabinets", "新增", key, row, submitter="admin")

        changes = get_pending_changes()
        with storage.db() as conn:
            services.apply_review_action(conn, changes[0], {"action": "reject", "reason": "不需要"}, reviewer="admin")

        cabinets = {(c["机房"], c["机柜"]) for c in storage.read_records("cabinets")}
        assert ("机房A", "C9") not in cabinets

    def test_pending_state_overlay(self, app_env, admin):
        add_official("cabinets", {"机房": "机房A", "机柜": "C1", "机位": "1-10"})
        # 正式 1 条 + 待审批修改 1 条：叠加视图按审批后数据显示
        modify = normalize_row("cabinets", {"机房": "机房A", "机柜": "C1", "机位": "1-12", "瓦数(kw)": "5"})
        services.submit_change("cabinets", "修改", "机房A|C1", modify, submitter="admin")

        preview = services.pending_state_records("cabinets", admin)
        assert len(preview) == 1
        assert preview[0]["_pending_operation"] == "修改"
        assert preview[0]["机位"] == "1-12"

        # 正式视图仍是旧值
        official = services.records_for_state("cabinets", "official", admin)
        assert official[0]["机位"] == "1-10"


class TestServerValidation:
    def test_server_slot_out_of_range(self, app_env, admin):
        add_official("cabinets", {"机房": "机房A", "机柜": "C1", "机位": "1-10"})
        _, errors = services.validate_record("servers", {"序列号": "S1", "所在机房": "机房A", "所属机柜": "C1", "所属机位": "11-12"}, None)
        assert any("超出机柜登记范围" in e for e in errors)

    def test_server_slot_conflict(self, app_env, admin):
        add_official("cabinets", {"机房": "机房A", "机柜": "C1", "机位": "1-10", "管理ip网段": "10.1.1.*", "交易ip网段": "10.1.2.*", "带外ip网段": "10.1.3.*"})
        add_official("servers", {"序列号": "S1", "所在机房": "机房A", "所属机柜": "C1", "所属机位": "1-2", "管理ip": "10.1.1.1", "交易ip": "10.1.2.1", "带外ip": "10.1.3.1"})
        _, errors = services.validate_record("servers", {"序列号": "S2", "所在机房": "机房A", "所属机柜": "C1", "所属机位": "2-3", "管理ip": "10.1.1.9", "交易ip": "10.1.2.9", "带外ip": "10.1.3.9"}, None)
        assert any("已被服务器 S1 占用" in e for e in errors)

    def test_same_kind_ip_conflict(self, app_env, admin):
        add_official("cabinets", {"机房": "机房A", "机柜": "C1", "机位": "1-10", "管理ip网段": "10.1.1.*", "交易ip网段": "10.1.2.*", "带外ip网段": "10.1.3.*"})
        add_official("servers", {"序列号": "S1", "所在机房": "机房A", "所属机柜": "C1", "所属机位": "1-2", "管理ip": "10.1.1.1", "交易ip": "10.1.2.1", "带外ip": "10.1.3.1"})
        _, errors = services.validate_record("servers", {"序列号": "S2", "所在机房": "机房A", "所属机柜": "C1", "所属机位": "3-4", "管理ip": "10.1.1.1", "交易ip": "10.1.2.9", "带外ip": "10.1.3.9"}, None)
        assert any("管理ip 10.1.1.1 已被服务器 S1 使用" in e for e in errors)

    def test_auto_assign_ips(self, app_env, admin):
        add_official("cabinets", {"机房": "机房A", "机柜": "C1", "机位": "1-10", "管理ip网段": "10.1.1.*", "交易ip网段": "10.1.2.*", "带外ip网段": "10.1.3.*"})
        add_official("servers", {"序列号": "S1", "所在机房": "机房A", "所属机柜": "C1", "所属机位": "1-2", "管理ip": "10.1.1.1", "交易ip": "10.1.2.1", "带外ip": "10.1.3.1"})
        ips = services.auto_assign_ips("机房A", "C1")
        assert ips == {"管理ip": "10.1.1.2", "交易ip": "10.1.2.2", "带外ip": "10.1.3.2"}


class TestRackRequestFlow:
    def _submit_rack(self, user, serial="SRV-1"):
        data = services.normalize_rack_request({"序列号": serial, "请求类型": "上架", "是否重装系统": "是", "客户id": "1", "所在机房": "机房A"})
        errors = services.validate_rack_request(data)
        assert errors == []
        with storage.db() as conn:
            request_id = services.insert_rack_request(conn, data, user)
        return request_id

    def test_rack_flow_allocation_then_implementation(self, app_env, admin):
        add_official("customers", {"客户id": "1", "名称": "客户一"})
        add_official("cabinets", {"机房": "机房A", "机柜": "C1", "机位": "1-10", "管理ip网段": "10.1.1.*", "交易ip网段": "10.1.2.*", "带外ip网段": "10.1.3.*"})

        request_id = self._submit_rack(admin)

        with storage.db() as conn:
            row = conn.execute("SELECT * FROM rack_requests WHERE id=?", (request_id,)).fetchone()
            assert row["stage"] == "allocation"
            server_data = services.rack_server_draft(row, {
                "所属机柜": "C1",
                "所属机位": "1-2",
                "管理ip": "10.1.1.5",
                "交易ip": "10.1.2.5",
                "带外ip": "10.1.3.5",
            })
            services.notify_rack_stage  # 确认可导入
            conn.execute("""UPDATE rack_requests
                SET stage='implementation',stage_data=? WHERE id=?""",
                (json.dumps(server_data, ensure_ascii=False), request_id))
            row = conn.execute("SELECT * FROM rack_requests WHERE id=?", (request_id,)).fetchone()
            message, errors = services.complete_rack_request(conn, row, admin, server_data)
        assert errors == []
        assert "已完成" in message

        serials = {s["序列号"] for s in storage.read_records("servers")}
        assert "SRV-1" in serials

    def test_rack_offline_request(self, app_env, admin):
        add_official("customers", {"客户id": "1", "名称": "客户一"})
        add_official("cabinets", {"机房": "机房A", "机柜": "C1", "机位": "1-10"})
        add_official("servers", {"序列号": "SRV-9", "客户id": "1", "所在机房": "机房A", "所属机柜": "C1", "所属机位": "1-2"})

        data = services.normalize_rack_request({"序列号": "SRV-9", "请求类型": "下架", "是否重装系统": "是"})
        errors = services.validate_rack_request(data)
        assert errors == []
        assert data["客户id"] == "1"      # 下架自动补齐
        assert data["所在机房"] == "机房A"

        with storage.db() as conn:
            request_id = services.insert_rack_request(conn, data, admin)
            row = conn.execute("SELECT * FROM rack_requests WHERE id=?", (request_id,)).fetchone()
            assert row["stage"] == "implementation"  # 下架直接进具体实施
            message, errors = services.complete_rack_request(conn, row, admin, {})
        assert errors == []
        serials = {s["序列号"] for s in storage.read_records("servers")}
        assert "SRV-9" not in serials


class TestSlotImplementationOccupancy:
    """机位在途占用：分配已完成、待具体实施的上架请求锁定机位，不允许再分配。"""

    def _setup_cabinet(self):
        add_official("customers", {"客户id": "1", "名称": "客户一"})
        add_official("cabinets", {"机房": "机房A", "机柜": "C1", "机位": "1-10", "管理ip网段": "10.1.1.*", "交易ip网段": "10.1.2.*", "带外ip网段": "10.1.3.*"})

    def _rack_in_implementation(self, admin, serial, slot="3-4"):
        data = services.normalize_rack_request({"序列号": serial, "请求类型": "上架", "是否重装系统": "是", "客户id": "1", "所在机房": "机房A"})
        with storage.db() as conn:
            request_id = services.insert_rack_request(conn, data, admin)
        server_data = {
            "客户id": "1", "所在机房": "机房A", "所属机柜": "C1", "所属机位": slot,
            "管理ip": "10.1.1.5", "交易ip": "10.1.2.5", "带外ip": "10.1.3.5",
        }
        with storage.db() as conn:
            conn.execute(
                "UPDATE rack_requests SET stage='implementation',stage_data=? WHERE id=?",
                (json.dumps(server_data, ensure_ascii=False), request_id),
            )
        return request_id, server_data

    def test_allocation_rejects_slot_held_by_implementation_request(self, app_env, admin):
        self._setup_cabinet()
        request_id, server_data = self._rack_in_implementation(admin, "SRV-1", "3-4")

        # 另一条上架请求试图分到重叠机位 2-4（覆盖待实施的 3）
        errors = services.validate_rack_allocation({
            "序列号": "SRV-2", "客户id": "1", "所在机房": "机房A", "所属机柜": "C1", "所属机位": "2-4",
            "管理ip": "10.1.1.9", "交易ip": "10.1.2.9", "带外ip": "10.1.3.9",
        })
        assert any("已被待具体实施服务器 SRV-1 占用" in e for e in errors)

    def test_allocation_allows_disjoint_slot(self, app_env, admin):
        self._setup_cabinet()
        self._rack_in_implementation(admin, "SRV-1", "3-4")

        errors = services.validate_rack_allocation({
            "序列号": "SRV-2", "客户id": "1", "所在机房": "机房A", "所属机柜": "C1", "所属机位": "5-6",
            "管理ip": "10.1.1.9", "交易ip": "10.1.2.9", "带外ip": "10.1.3.9",
        })
        assert not any("待具体实施" in e for e in errors)

    def test_allocation_excludes_self_request(self, app_env, admin):
        self._setup_cabinet()
        request_id, server_data = self._rack_in_implementation(admin, "SRV-1", "3-4")

        # 重新校验同一条请求自身：排除自己后不应报冲突
        errors = services.validate_rack_allocation(server_data, request_id)
        assert not any("待具体实施" in e for e in errors)

    def test_cabinet_options_marks_slot_unselectable(self, app_env, admin):
        self._setup_cabinet()
        self._rack_in_implementation(admin, "SRV-1", "3-4")

        options = services.cabinet_options()
        cabinet = next(c for r in options["rooms"] if r["name"] == "机房A" for c in r["cabinets"] if c["name"] == "C1")
        slot3 = next(s for s in cabinet["slots"] if s["slot"] == "3")
        assert slot3["status"] == "pending_implementation"
        assert slot3["selectable"] is False
        assert slot3["server"] == "SRV-1"
        slot6 = next(s for s in cabinet["slots"] if s["slot"] == "6")
        assert slot6["status"] == "free"

    def test_implementation_states_exclude_request_id(self, app_env, admin):
        self._setup_cabinet()
        request_id, _ = self._rack_in_implementation(admin, "SRV-1", "3-4")

        states = services.implementation_slot_states(exclude_request_id=request_id)
        assert states == {}
        states_all = services.implementation_slot_states()
        assert ("机房A", "C1") in states_all
        assert 3 in states_all[("机房A", "C1")]


class TestCustomerCascade:
    def test_customer_id_change_cascades(self, app_env, admin):
        add_official("customers", {"客户id": "1", "名称": "客户一"})
        add_official("servers", {"序列号": "S1", "客户id": "1", "客户名称": "客户一", "所在机房": "", "所属机柜": "", "所属机位": ""})

        with storage.db() as conn:
            services.cascade_customer_change(conn, "1", "2", "客户一改", "x")

        servers = storage.read_records("servers")
        assert servers[0]["客户id"] == "2"
        assert servers[0]["客户名称"] == "客户一改"
