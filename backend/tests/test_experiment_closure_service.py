"""实验结案服务与接口链路测试。

验证三件事：
1. 通用资源接口 + 结案专用接口共用同一份账本，实验行、批次行、指标对比行同步；
2. 结案 / 迟到回传 / 财务复算的处置互斥，原订单只被领用一次；
3. 日报只输出依据完整的实验结果。
"""
from __future__ import annotations

import json

import pytest

from app.main import app
from app.services import SERVICES
from app.services.valid_conversion_batch import LEDGERS, ValidConversionBatchService
from app.store import store


@pytest.fixture(autouse=True)
def clean_state():
    """每个用例从干净账本开始，避免服务单例互相污染。"""
    LEDGERS.clear()
    for module in ("valid_conversion_batch", "metric_compare"):
        store._tables[module] = []
    store._tables["valid_conversion_batch"] = []
    yield
    LEDGERS.clear()


@pytest.fixture
def batch_service() -> ValidConversionBatchService:
    return SERVICES["valid_conversion_batch"]  # type: ignore[return-value]


@pytest.fixture
def experiment() -> dict:
    return next(
        row for row in store.rows("ab_experiment") if row["方案编号"] == "AB_E-0003"
    )


def _setup_store_experiment(service: ValidConversionBatchService) -> None:
    """门店实验：15:00 停收；甲 14:50 入组，乙 15:03 入组。"""
    service.configure_round(
        "AB_E-0003", 1, "2026-11-24T14:00:00", "2026-11-24T15:00:00",
    )
    service.enroll("AB_E-0003", "甲", "2026-11-24T14:50:00")
    service.enroll("AB_E-0003", "乙", "2026-11-24T15:03:00")


class TestServiceFlows:
    def test_terminate_closes_batch_and_freezes_snapshot(self, batch_service):
        _setup_store_experiment(batch_service)
        experiment_service = SERVICES["ab_experiment"]
        entry, message = experiment_service.run_action(
            3, "终止实验", "2026-11-24T15:05:00",
        )
        assert entry is not None
        assert entry["status"] == "已结束"
        assert entry["结案状态"] == "已结案"
        assert entry["结案轮次"] == 1
        assert entry["入组人数"] == 1
        assert entry["有效购买数"] == 0
        assert "1 人" in message
        assert LEDGERS["AB_E-0003"].batches[1].status.value == "已结案"
        assert LEDGERS["AB_E-0003"].batches[2].status.value == "收集中"

    def test_late_report_after_terminate_marks_experiment_reaudit(self, batch_service):
        _setup_store_experiment(batch_service)
        experiment_service = SERVICES["ab_experiment"]
        experiment_service.run_action(3, "终止实验", "2026-11-24T15:05:00")

        result = batch_service.report_conversion(
            "AB_E-0003", order_id="ORD-J", user_id="甲",
            ordered_at="2026-11-24T14:58:00", reported_at="2026-11-24T15:05:00",
        )
        assert result["claim"]["disposition"] == "留作更正"

        experiment = next(row for row in store.rows("ab_experiment") if row["方案编号"] == "AB_E-0003")
        assert experiment["结案状态"] == "补审中"
        assert experiment["当次结案订单"] == ""
        assert experiment["待更正订单"] == "ORD-J"

        batch_row = next(row for row in store.rows("valid_conversion_batch") if row["实验轮次"] == 1)
        assert batch_row["有效购买数"] == 0
        assert batch_row["未处置差异"] == 1

    def test_finance_recalc_moves_order_out_of_experiment_scope(self, batch_service):
        _setup_store_experiment(batch_service)
        experiment_service = SERVICES["ab_experiment"]
        experiment_service.run_action(3, "终止实验", "2026-11-24T15:05:00")
        batch_service.report_conversion(
            "AB_E-0003", order_id="ORD-J", user_id="甲",
            ordered_at="2026-11-24T14:58:00", reported_at="2026-11-24T15:05:00",
        )
        batch_service.handoff("AB_E-0003")
        payload = batch_service.finance_recalc("AB_E-0003", "ORD-J", include=True)

        assert payload["state"]["counted_in_closure"] is False
        assert payload["state"]["counted_in_correction"] is True
        assert payload["counts"] == {
            "experiment_orders": 0, "finance_orders": 1,
            "total_claimed": 1, "overlap": 0,
        }
        experiment = next(row for row in store.rows("ab_experiment") if row["方案编号"] == "AB_E-0003")
        assert experiment["已更正订单"] == "ORD-J"
        assert experiment["待更正订单"] == ""

    def test_handoff_is_reflected_on_batch_row(self, batch_service):
        _setup_store_experiment(batch_service)
        experiment_service = SERVICES["ab_experiment"]
        experiment_service.run_action(3, "终止实验", "2026-11-24T15:05:00")
        batch_service.report_conversion(
            "AB_E-0003", order_id="ORD-J", user_id="甲",
            ordered_at="2026-11-24T14:58:00", reported_at="2026-11-24T15:05:00",
        )
        batch_service.handoff("AB_E-0003")
        batch_row = next(row for row in store.rows("valid_conversion_batch") if row["实验轮次"] == 1)
        assert batch_row["未处置差异"] == 0
        assert batch_row["依据状态"] == "依据完整"


class TestMetricCompareIntegration:
    def test_metric_compare_uses_batch_denominator(self, batch_service):
        _setup_store_experiment(batch_service)
        batch_service.report_conversion(
            "AB_E-0003", order_id="ORD-J", user_id="甲",
            ordered_at="2026-11-24T14:58:00", reported_at="2026-11-24T14:59:00",
        )
        batch_service.close("AB_E-0003", "2026-11-24T15:05:00")

        payload = batch_service.comparison_payload("AB_E-0003")
        comparison = payload["comparison"]
        assert comparison["入组人数"] == 1
        assert comparison["有效购买数"] == 1
        assert comparison["分母用户"] == ["甲"]
        assert comparison["对比状态"] == "已完成"

        row = payload["metric_compare_row"]
        assert row["关联批次"] == "VCB_AB_E-0003_R1"
        assert row["基准值"] == 1
        assert row["变化幅度"] == 1
        assert row["分母用户"] == "甲"
        assert row["status"] == "已完成"

    def test_metric_compare_service_recomputes_from_linked_batch(self, batch_service):
        _setup_store_experiment(batch_service)
        payload = batch_service.comparison_payload("AB_E-0003")
        row = payload["metric_compare_row"]
        compare_service = SERVICES["metric_compare"]

        # 结案后补一笔有效转化，重算对比时分子应更新，分母保持不变
        batch_service.report_conversion(
            "AB_E-0003", order_id="ORD-J", user_id="甲",
            ordered_at="2026-11-24T14:58:00", reported_at="2026-11-24T14:59:00",
        )
        batch_service.close("AB_E-0003", "2026-11-24T15:05:00")
        updated, message = compare_service.run_action(row["id"], "重算对比")
        assert updated is not None
        assert updated["基准值"] == 1
        assert updated["变化幅度"] == 1
        assert "重算" in message

    def test_incomplete_basis_marks_compare_missing(self, batch_service):
        _setup_store_experiment(batch_service)
        batch_service.close("AB_E-0003", "2026-11-24T15:05:00")
        batch_service.report_conversion(
            "AB_E-0003", order_id="ORD-J", user_id="甲",
            ordered_at="2026-11-24T14:58:00", reported_at="2026-11-24T15:05:00",
        )
        row = batch_service.comparison_payload("AB_E-0003")["metric_compare_row"]
        assert row["对比状态"] == "数据缺失"
        assert row["status"] == "数据缺失"


class TestDailyReportApi:
    def test_daily_report_excludes_incomplete_and_late_enrollee(self, batch_service):
        _setup_store_experiment(batch_service)
        batch_service.close("AB_E-0003", "2026-11-24T15:05:00")
        batch_service.report_conversion(
            "AB_E-0003", order_id="ORD-J", user_id="甲",
            ordered_at="2026-11-24T14:58:00", reported_at="2026-11-24T15:05:00",
        )
        report = batch_service.daily_report("AB_E-0003", "2026-11-24")
        assert report["experiments"] == []
        assert {item["round_no"] for item in report["skipped"]} >= {1, 2}
        assert report["complete"] is False
        assert report["incomplete_rounds"] == [1]
        assert report["pending_rounds"] == [2]

        batch_service.handoff("AB_E-0003")
        report = batch_service.daily_report("AB_E-0003", "2026-11-24")
        experiment = report["experiments"][0]
        assert experiment["enrolled_user_ids"] == ["甲"]
        assert experiment["denominator"] == 1
        assert experiment["numerator"] == 0
        # 后续轮次还没结案只是没到期，不阻断已结案轮次的日报
        assert report["complete"] is True
        assert report["pending_rounds"] == [2]

    def test_http_daily_report_and_claim_state(self, batch_service):
        _setup_store_experiment(batch_service)
        batch_service.close("AB_E-0003", "2026-11-24T15:05:00")
        batch_service.report_conversion(
            "AB_E-0003", order_id="ORD-J", user_id="甲",
            ordered_at="2026-11-24T14:58:00", reported_at="2026-11-24T15:05:00",
        )

        status, payload = _http("GET", "/api/experiment-closure/daily-report?experiment_id=AB_E-0003")
        assert status == 200
        assert payload["experiments"] == []
        assert payload["complete"] is False

        status, claim = _http("GET", "/api/experiment-closure/AB_E-0003/claims/ORD-J")
        assert status == 200
        assert claim["disposition"] == "留作更正"
        assert claim["counted_in_closure"] is False
        assert claim["counted_in_correction"] is False


def _http(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    """不依赖 httpx，直接用 ASGI 调一次接口。"""
    import asyncio

    payload = json.dumps(body).encode() if body is not None else b""
    headers = [(b"content-type", b"application/json")]
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path.split("?")[0],
        "raw_path": path.encode(),
        "query_string": path.split("?", 1)[1].encode() if "?" in path else b"",
        "headers": headers,
        "client": ("test", 123),
        "server": ("testserver", 80),
    }
    messages: list[dict] = []

    async def receive() -> dict:
        return {"type": "http.request", "body": payload, "more_body": False}

    async def send(message: dict) -> None:
        messages.append(message)

    asyncio.run(app(scope, receive, send))
    start = next(message for message in messages if message["type"] == "http.response.start")
    body_bytes = b"".join(
        message.get("body", b"") for message in messages
        if message["type"] == "http.response.body"
    )
    return start["status"], json.loads(body_bytes or b"{}")
