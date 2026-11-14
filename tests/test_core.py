"""core 层纯函数测试：机位解析、IP、权限矩阵、排序、过滤条件解析。"""
import pytest

from app.core import (
    ENTITIES,
    allocatable_ip,
    can_mutate_entity,
    can_view_entity,
    capabilities_for,
    compare_sort_values,
    filter_records_for_user,
    is_valid_ipv4_or_cidr,
    parse_filters,
    parse_ip_range,
    parse_sorts,
    positions,
    sort_records,
)

ADMIN = {"用户id": "a", "身份": "管理人员", "业务单位": "", "所属机房": ""}
ROOM = {"用户id": "r", "身份": "机房人员", "业务单位": "", "所属机房": "机房A"}
BIZ = {"用户id": "b", "身份": "业务人员", "业务单位": "单位A", "所属机房": ""}


class TestPositions:
    def test_range(self):
        assert positions("9-10") == [10, 9]

    def test_reversed_range(self):
        assert positions("10-9") == [10, 9]

    def test_invalid(self):
        assert positions("abc") == []
        assert positions("") == []
        assert positions("9") == []


class TestIp:
    def test_allocatable(self):
        assert allocatable_ip("10.0.0.5") == "10.0.0.5"
        assert allocatable_ip("10.0.0.0") == ""
        assert allocatable_ip("10.0.0.255") == ""
        assert allocatable_ip("not-ip") == ""

    def test_valid_ipv4_or_cidr(self):
        assert is_valid_ipv4_or_cidr("10.71.141.193")
        assert is_valid_ipv4_or_cidr("10.71.141.193/25")
        assert not is_valid_ipv4_or_cidr("999.1.1.1")
        assert not is_valid_ipv4_or_cidr("::1")

    def test_parse_ip_range_wildcard(self):
        ips = parse_ip_range("10.1.1.*")
        assert ips[0] == "10.1.1.1"
        assert ips[-1] == "10.1.1.254"
        assert len(ips) == 254

    def test_parse_ip_range_cidr(self):
        ips = parse_ip_range("10.1.1.0/30")
        assert ips == ["10.1.1.1", "10.1.1.2"]

    def test_parse_ip_range_invalid(self):
        with pytest.raises(ValueError):
            parse_ip_range("300.1.1.1-400.1.1.1")

    def test_parse_ip_range_not_an_ip_returns_empty(self):
        assert parse_ip_range("10.1.1.x") == []


class TestPermissions:
    def test_capabilities(self):
        assert "review_operations" in capabilities_for(ADMIN)
        assert "review_operations" not in capabilities_for(ROOM)
        assert "review_rack_requests" in capabilities_for(ROOM)
        assert capabilities_for(BIZ) == ["submit_rack_request"]

    def test_view_entity(self):
        for entity in ("customers", "cabinets", "servers"):
            assert can_view_entity(entity, ADMIN)
        assert can_view_entity("customers", ROOM) is False
        assert can_view_entity("customers", BIZ) is True
        assert can_view_entity("cabinets", BIZ) is False

    def test_mutate_entity(self):
        assert can_mutate_entity("servers", BIZ) is True
        assert can_mutate_entity("customers", BIZ) is True
        assert can_mutate_entity("cabinets", BIZ) is False
        assert can_mutate_entity("servers", ROOM) is True

    def test_filter_records_for_user_room(self):
        records = [
            {"机房": "机房A", "机柜": "C1"},
            {"机房": "机房B", "机柜": "C2"},
        ]
        assert filter_records_for_user("cabinets", records, ROOM) == [{"机房": "机房A", "机柜": "C1"}]

    def test_filter_records_for_user_biz(self):
        records = [
            {"归属单位": "单位A", "序列号": "S1"},
            {"归属单位": "单位B", "序列号": "S2"},
        ]
        assert filter_records_for_user("servers", records, BIZ) == [{"归属单位": "单位A", "序列号": "S1"}]

    def test_admin_sees_all(self):
        records = [{"机房": "机房A"}, {"机房": "机房B"}]
        assert filter_records_for_user("cabinets", records, ADMIN) == records


class TestQueryParsing:
    def test_parse_filters_ok(self):
        filters = parse_filters("cabinets", {"filters": [{"field": "机房", "pattern": "A"}]})
        assert len(filters) == 1

    def test_parse_filters_pending_status_field(self):
        filters = parse_filters("cabinets", {"filters": [{"field": "在途状态", "pattern": "待审批"}]})
        assert len(filters) == 1

    def test_parse_filters_unknown_field(self):
        with pytest.raises(ValueError):
            parse_filters("cabinets", {"filters": [{"field": "不存在列", "pattern": "x"}]})

    def test_parse_sorts_bad_direction(self):
        with pytest.raises(ValueError):
            parse_sorts("cabinets", {"sorts": [{"field": "机房", "direction": "up"}]})


class TestSort:
    def test_natural_sort(self):
        assert compare_sort_values("机柜2", "机柜10", "asc") < 0
        assert compare_sort_values("机柜10", "机柜2", "asc") > 0

    def test_empty_last(self):
        assert compare_sort_values("", "x", "asc") > 0

    def test_sort_records_desc(self):
        rows = [{"机柜": "B"}, {"机柜": "A"}]
        result = sort_records(rows, [("机柜", "desc")])
        assert [r["机柜"] for r in result] == ["B", "A"]


class TestEntityMeta:
    def test_headers_match_official(self):
        # schema 兼容性红线：表头不可意外变更
        assert ENTITIES["customers"]["headers"][0] == "客户id"
        assert "序列号" in ENTITIES["servers"]["key"]
        assert ENTITIES["cabinets"]["key"] == ["机房", "机柜"]
