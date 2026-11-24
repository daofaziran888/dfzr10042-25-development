"""实验结案 · 有效转化批次场景测试。

覆盖需求里的门店实验：15:00 停收，甲 14:50 入组、14:58 下单但订单在负责人
点结案时才回传；乙 15:03 才入组。
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.experiment_closure import (
    ClaimOwner,
    ConversionDisposition,
    ExperimentClosureLedger,
    ExperimentRound,
    build_daily_report,
    build_metric_comparison,
    parse_datetime,
)


def dt(text: str) -> datetime:
    return datetime.fromisoformat(f"2026-11-24T{text}")


@pytest.fixture
def ledger() -> ExperimentClosureLedger:
    """门店实验：15:00 停收，停收后自动进入下一轮（16:00 停收）。"""
    return ExperimentClosureLedger(
        "EXP_STORE",
        rounds=[ExperimentRound(1, dt("14:00:00"), dt("15:00:00"))],
        round_duration=timedelta(hours=1),
    )


class TestEnrollmentAndDenominator:
    def test_late_enrollment_goes_to_next_round(self, ledger):
        first = ledger.enroll("甲", dt("14:50:00"))
        second = ledger.enroll("乙", dt("15:03:00"))

        assert first.round_no == 1
        assert second.round_no == 2
        assert ledger.batches[1].enrolled_user_ids() == ["甲"]
        assert ledger.batches[2].enrolled_user_ids() == ["乙"]

    def test_enrollment_after_cutoff_does_not_change_old_denominator(self, ledger):
        ledger.enroll("甲", dt("14:50:00"))
        ledger.close_round(1, dt("15:05:00"))
        ledger.enroll("乙", dt("15:03:00"))

        assert ledger.batches[1].recompute()["denominator"] == 1
        assert ledger.batches[1].recompute()["enrolled_user_ids"] == ["甲"]

    def test_enrollment_record_is_idempotent(self, ledger):
        first = ledger.enroll("甲", dt("14:50:00"))
        again = ledger.enroll("甲", dt("14:51:00"))
        assert first.enrolled_at == again.enrolled_at == dt("14:50:00")


class TestConversionTiming:
    def test_order_before_enrollment_is_rejected(self, ledger):
        ledger.enroll("甲", dt("14:50:00"))
        _, claim, _ = ledger.register_conversion(
            order_id="ORD_A", user_id="甲",
            ordered_at=dt("14:45:00"), reported_at=dt("14:46:00"),
        )
        assert claim.valid is False
        assert "下单早于入组" in claim.reason

    def test_order_after_cutoff_is_rejected(self, ledger):
        ledger.enroll("甲", dt("14:50:00"))
        _, claim, _ = ledger.register_conversion(
            order_id="ORD_A", user_id="甲",
            ordered_at=dt("15:01:00"), reported_at=dt("15:02:00"),
        )
        assert claim.valid is False
        assert "下单晚于停收" in claim.reason

    def test_unenrolled_user_is_rejected(self, ledger):
        _, claim, _ = ledger.register_conversion(
            order_id="ORD_X", user_id="乙",
            ordered_at=dt("14:58:00"), reported_at=dt("14:59:00"),
        )
        assert claim.valid is False
        assert claim.disposition == ConversionDisposition.REJECTED

    def test_valid_order_reported_before_close_counts(self, ledger):
        ledger.enroll("甲", dt("14:50:00"))
        ledger.register_conversion(
            order_id="ORD_A", user_id="甲",
            ordered_at=dt("14:58:00"), reported_at=dt("14:59:00"),
        )
        snapshot = ledger.close_round(1, dt("15:05:00"))
        assert snapshot["denominator"] == 1
        assert snapshot["numerator"] == 1
        assert snapshot["purchase_order_ids"] == ["ORD_A"]
        assert ledger.claim_state("ORD_A")["counted_in_closure"] is True


class TestLateReportAfterClose:
    def test_late_report_is_deferred_and_not_written_back(self, ledger):
        ledger.enroll("甲", dt("14:50:00"))
        snapshot = ledger.close_round(1, dt("15:05:00"))
        assert snapshot["numerator"] == 0

        _, claim, _ = ledger.register_conversion(
            order_id="ORD_A", user_id="甲",
            ordered_at=dt("14:58:00"), reported_at=dt("15:05:00"),
        )
        assert claim.valid is True
        assert claim.disposition == ConversionDisposition.DEFERRED
        assert claim.counted_in_closure is False
        # 结案快照冻结，不因迟到回传而回写
        assert ledger.batches[1].close_snapshot["numerator"] == 0
        assert ledger.claim_state("ORD_A")["in_open_diff"] is True

    def test_late_enrollee_not_counted_in_old_experiment(self, ledger):
        ledger.enroll("甲", dt("14:50:00"))
        ledger.close_round(1, dt("15:05:00"))
        ledger.enroll("乙", dt("15:03:00"))
        ledger.register_conversion(
            order_id="ORD_B", user_id="乙",
            ordered_at=dt("15:10:00"), reported_at=dt("15:10:00"),
        )
        first = ledger.batches[1].recompute()
        assert first["denominator"] == 1
        assert first["enrolled_user_ids"] == ["甲"]
        assert "ORD_B" not in first["purchase_order_ids"]
        # 乙的订单落在自己实际入组的第 2 轮
        assert ledger.batches[2].claims["ORD_B"].round_no == 2

    def test_late_report_withdraws_batch_from_daily_report(self, ledger):
        ledger.enroll("甲", dt("14:50:00"))
        ledger.close_round(1, dt("15:05:00"))
        ledger.register_conversion(
            order_id="ORD_A", user_id="甲",
            ordered_at=dt("14:58:00"), reported_at=dt("15:05:00"),
        )
        report = ledger.daily_report("2026-11-24")
        assert report["experiments"] == []
        assert report["complete"] is False
        assert report["skipped"][0]["unresolved_order_ids"] == ["ORD_A"]

        ledger.handoff_unresolved()
        report = ledger.daily_report("2026-11-24")
        assert report["complete"] is True
        assert report["experiments"][0]["numerator"] == 0
        assert report["experiments"][0]["denominator"] == 1

        ledger.finance_recalc("ORD_A", include=True)
        report = ledger.daily_report("2026-11-24")
        assert [item["original_order_id"] for item in report["corrections"]] == ["ORD_A"]
        assert report["experiments"][0]["numerator"] == 0


class TestIdempotencyAndOwnership:
    def test_duplicate_original_order_counted_once(self, ledger):
        ledger.enroll("甲", dt("14:50:00"))
        ledger.register_conversion(
            order_id="ORD_A", user_id="甲",
            ordered_at=dt("14:58:00"), reported_at=dt("14:59:00"),
        )
        _, duplicate, is_duplicate = ledger.register_conversion(
            order_id="ORD_A", user_id="甲",
            ordered_at=dt("14:58:00"), reported_at=dt("15:06:00"),
        )
        assert is_duplicate is True
        assert duplicate.duplicate_submissions == 1
        snapshot = ledger.close_round(1, dt("15:07:00"))
        assert snapshot["numerator"] == 1
        assert snapshot["purchase_order_ids"] == ["ORD_A"]
        assert ledger.counts()["overlap"] == 0

    def test_claim_toggles_between_sides_without_double_counting(self, ledger):
        ledger.enroll("甲", dt("14:50:00"))
        ledger.register_conversion(
            order_id="ORD_A", user_id="甲",
            ordered_at=dt("14:58:00"), reported_at=dt("14:59:00"),
        )
        ledger.close_round(1, dt("15:05:00"))

        state = ledger.transfer_claim("ORD_A", ClaimOwner.FINANCE.value)
        assert state["owner"] == "finance"
        assert state["counted_in_closure"] is False
        counts = ledger.counts()
        assert counts == {
            "experiment_orders": 0, "finance_orders": 0,
            "total_claimed": 0, "overlap": 0,
        }

        state = ledger.transfer_claim("ORD_A", ClaimOwner.EXPERIMENT.value)
        assert state["owner"] == "experiment"
        assert state["counted_in_closure"] is True
        counts = ledger.counts()
        assert counts["experiment_orders"] == 1
        assert counts["finance_orders"] == 0
        assert counts["overlap"] == 0

        # 再次切到财务侧再切回结案侧，仍然只有一笔
        ledger.transfer_claim("ORD_A", ClaimOwner.FINANCE.value)
        ledger.transfer_claim("ORD_A", ClaimOwner.EXPERIMENT.value)
        counts = ledger.counts()
        assert counts["experiment_orders"] == 1
        assert counts["finance_orders"] == 0
        assert counts["total_claimed"] == 1
        assert counts["overlap"] == 0
        assert ledger.batches[1].close_snapshot["numerator"] == 1

    def test_late_order_cannot_be_appended_to_frozen_closure(self, ledger):
        ledger.enroll("甲", dt("14:50:00"))
        ledger.close_round(1, dt("15:05:00"))
        ledger.register_conversion(
            order_id="ORD_A", user_id="甲",
            ordered_at=dt("14:58:00"), reported_at=dt("15:05:00"),
        )
        ledger.handoff_unresolved()
        with pytest.raises(ValueError):
            ledger.transfer_claim("ORD_A", ClaimOwner.EXPERIMENT.value)
        assert ledger.claim_state("ORD_A")["counted_in_closure"] is False

    def test_duplicate_reported_after_handoff_does_not_reopen_closure(self, ledger):
        ledger.enroll("甲", dt("14:50:00"))
        ledger.close_round(1, dt("15:05:00"))
        ledger.register_conversion(
            order_id="ORD_A", user_id="甲",
            ordered_at=dt("14:58:00"), reported_at=dt("15:05:00"),
        )
        ledger.handoff_unresolved()
        ledger.finance_recalc("ORD_A", include=True)
        _, claim, duplicate = ledger.register_conversion(
            order_id="ORD_A", user_id="甲",
            ordered_at=dt("14:58:00"), reported_at=dt("15:20:00"),
        )
        assert duplicate is True
        assert claim.disposition == ConversionDisposition.FINANCE_CONFIRMED
        assert claim.duplicate_submissions == 1
        assert ledger.batches[1].close_snapshot["numerator"] == 0


class TestMetricComparison:
    def test_comparison_uses_batch_denominator_and_real_purchases(self, ledger):
        ledger.enroll("甲", dt("14:50:00"))
        ledger.enroll("丙", dt("14:55:00"))
        ledger.register_conversion(
            order_id="ORD_A", user_id="甲",
            ordered_at=dt("14:58:00"), reported_at=dt("14:59:00"),
        )
        ledger.close_round(1, dt("15:05:00"))

        comparison = build_metric_comparison(ledger.batches[1])
        assert comparison["入组人数"] == 2
        assert comparison["有效购买数"] == 1
        assert comparison["转化率"] == 0.5
        assert comparison["依据状态"] == "依据完整"
        assert comparison["分母用户"] == ["丙", "甲"]
        assert comparison["分子订单"] == ["ORD_A"]

    def test_incomplete_basis_reports_missing_data(self, ledger):
        ledger.enroll("甲", dt("14:50:00"))
        ledger.close_round(1, dt("15:05:00"))
        ledger.register_conversion(
            order_id="ORD_A", user_id="甲",
            ordered_at=dt("14:58:00"), reported_at=dt("15:05:00"),
        )
        comparison = build_metric_comparison(ledger.batches[1])
        assert comparison["对比状态"] == "数据缺失"
        assert comparison["依据状态"] == "依据不完整"


class TestConcurrentCloseAndLateReport:
    def test_concurrent_operations_keep_single_count(self, ledger):
        import threading

        ledger.enroll("甲", dt("14:50:00"))
        barrier = threading.Barrier(2)
        results: list[object] = []

        def close_side():
            barrier.wait()
            results.append(("close", ledger.close_round(1, dt("15:05:00"))))

        def report_side():
            barrier.wait()
            results.append(("report", ledger.register_conversion(
                order_id="ORD_A", user_id="甲",
                ordered_at=dt("14:58:00"), reported_at=dt("15:05:00"),
            )))

        threads = [threading.Thread(target=close_side), threading.Thread(target=report_side)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        batch = ledger.batches[1]
        state = ledger.claim_state("ORD_A")
        if state["counted_in_closure"]:
            # 回传先到：订单进入当次结案，只计一次
            assert batch.close_snapshot["numerator"] == 1
        else:
            # 结案先到：订单留作更正，结案快照仍为 0
            assert batch.close_snapshot["numerator"] == 0
            assert state["in_open_diff"] is True
        assert ledger.counts()["overlap"] == 0
        assert state["duplicate_submissions"] == 0

    def test_daily_report_after_concurrent_close_never_uses_partial_data(self, ledger):
        import threading

        ledger.enroll("甲", dt("14:50:00"))
        ledger.enroll("乙", dt("15:03:00"))
        barrier = threading.Barrier(2)

        def close_side():
            barrier.wait()
            ledger.close_round(1, dt("15:05:00"))

        def report_side():
            barrier.wait()
            ledger.register_conversion(
                order_id="ORD_A", user_id="甲",
                ordered_at=dt("14:58:00"), reported_at=dt("15:05:00"),
            )

        threads = [threading.Thread(target=close_side), threading.Thread(target=report_side)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        report = ledger.daily_report("2026-11-24")
        # 第 2 轮（乙）尚未结案，必须被跳过；日报里出现的只能是依据完整的结果
        assert any(item["round_no"] == 2 for item in report["skipped"])
        assert all(item["basis_complete"] for item in report["experiments"])

        state = ledger.claim_state("ORD_A")
        if state["in_open_diff"]:
            assert report["experiments"] == []
            ledger.handoff_unresolved()
            report = ledger.daily_report("2026-11-24")

        assert len(report["experiments"]) == 1
        experiment = report["experiments"][0]
        assert experiment["round_no"] == 1
        assert experiment["denominator"] == 1
        assert experiment["enrolled_user_ids"] == ["甲"]
        assert "乙" not in experiment["enrolled_user_ids"]


class TestMultiLedgerDailyReport:
    def test_build_daily_report_excludes_incomplete_ledgers(self):
        ledger = ExperimentClosureLedger(
            "EXP_STORE",
            rounds=[ExperimentRound(1, dt("14:00:00"), dt("15:00:00"))],
        )
        ledger.enroll("甲", dt("14:50:00"))
        ledger.close_round(1, dt("15:05:00"))
        ledger.register_conversion(
            order_id="ORD_A", user_id="甲",
            ordered_at=dt("14:58:00"), reported_at=dt("15:05:00"),
        )
        report = build_daily_report([ledger], "2026-11-24")
        assert report["complete"] is False
        assert report["experiments"] == []


def test_parse_datetime_accepts_iso_and_datetime():
    assert parse_datetime("2026-11-24T14:50:00") == dt("14:50:00")
    assert parse_datetime(dt("14:50:00")) == dt("14:50:00")
    assert parse_datetime(None) is None
