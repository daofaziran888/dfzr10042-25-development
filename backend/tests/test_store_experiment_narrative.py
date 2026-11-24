"""门店实验复盘：按题面场景逐步核对结案口径。

15:00 停收的门店实验：
* 甲 14:50 入组、14:58 下单，订单直到负责人点结案才回传；
* 乙 15:03 才入组（属于下一轮，不能算进旧实验）。
"""
from __future__ import annotations

import threading

import pytest

from app.experiment_closure import (
    ClaimOwner,
    ConversionDisposition,
    ExperimentClosureLedger,
    ExperimentRound,
)
from app.experiment_closure import parse_datetime


def dt(text: str):
    return parse_datetime(f"2026-11-24T{text}")


@pytest.fixture
def store_ledger() -> ExperimentClosureLedger:
    ledger = ExperimentClosureLedger(
        "EXP_STORE",
        rounds=[ExperimentRound(1, dt("14:00:00"), dt("15:00:00"))],
    )
    ledger.enroll("甲", dt("14:50:00"))
    return ledger


class TestNarrative:
    def test_full_story(self, store_ledger):
        ledger = store_ledger
        # 乙在停收后才进组：归入下一轮，第一步就不再污染旧分母
        assert ledger.enroll("乙", dt("15:03:00")).round_no == 2

        # 负责人在 15:05 点结案：此时甲的订单还没回传
        snapshot = ledger.close_round(1, dt("15:05:00"))
        assert snapshot["denominator"] == 1
        assert snapshot["numerator"] == 0
        assert snapshot["enrolled_user_ids"] == ["甲"]

        # 甲的订单在结案时刻回传：属于迟到转化，留作更正
        _, claim, duplicate = ledger.register_conversion(
            order_id="ORD-甲-1", user_id="甲",
            ordered_at=dt("14:58:00"), reported_at=dt("15:05:00"),
        )
        assert duplicate is False
        assert claim.valid is True
        assert claim.disposition == ConversionDisposition.DEFERRED
        # 结案快照不被回写
        assert ledger.batches[1].close_snapshot["numerator"] == 0

        # 日报不使用依据不完整的结果
        report = ledger.daily_report("2026-11-24")
        assert report["experiments"] == []
        assert report["skipped"][0]["unresolved_order_ids"] == ["ORD-甲-1"]

        # 负责人把尚未处置的差异交财务复算
        moved = ledger.handoff_unresolved()
        assert [item.order_id for item in moved] == ["ORD-甲-1"]
        assert moved[0].owner == ClaimOwner.FINANCE.value

        # 读日报：结案口径仍是 1 人 / 0 笔；乙不进旧实验
        report = ledger.daily_report("2026-11-24")
        experiment = next(item for item in report["experiments"] if item["round_no"] == 1)
        assert experiment["denominator"] == 1
        assert experiment["numerator"] == 0
        assert experiment["enrolled_user_ids"] == ["甲"]
        assert "乙" not in experiment["enrolled_user_ids"]

        # 核对甲当前归属：已计入当次结案，还是留作这次更正
        state = ledger.claim_state("ORD-甲-1")
        assert state["counted_in_closure"] is False
        assert state["in_open_diff"] is True

        # 财务复算：甲计入本次更正
        ledger.finance_recalc("ORD-甲-1", include=True)
        state = ledger.claim_state("ORD-甲-1")
        assert state["counted_in_correction"] is True
        assert state["batch_id"] == "VCB_EXP_STORE_R1"
        assert ledger.counts() == {
            "experiment_orders": 0, "finance_orders": 1,
            "total_claimed": 1, "overlap": 0,
        }

        # 重复提交甲的原订单只记一次
        _, again, duplicate = ledger.register_conversion(
            order_id="ORD-甲-1", user_id="甲",
            ordered_at=dt("14:58:00"), reported_at=dt("15:20:00"),
        )
        assert duplicate is True
        assert again.duplicate_submissions == 1
        assert again.disposition == ConversionDisposition.FINANCE_CONFIRMED
        assert ledger.counts()["total_claimed"] == 1

    def test_closure_snapshot_is_used_by_daily_report_not_live_recount(self, store_ledger):
        ledger = store_ledger
        ledger.register_conversion(
            order_id="ORD-甲-1", user_id="甲",
            ordered_at=dt("14:58:00"), reported_at=dt("14:59:00"),
        )
        ledger.close_round(1, dt("15:05:00"))
        assert ledger.batches[1].close_snapshot["numerator"] == 1

        # 结案之后把甲切到财务侧：历史结案口径不变，只是一笔更正
        ledger.transfer_claim("ORD-甲-1", ClaimOwner.FINANCE.value)
        report = ledger.daily_report("2026-11-24")
        experiment = next(item for item in report["experiments"] if item["round_no"] == 1)
        assert experiment["numerator"] == 1
        assert experiment["close_snapshot_numerator"] == 1
        assert experiment["current_numerator"] == 0
        assert experiment["correction_order_ids"] == []
        assert experiment["pending_correction_order_ids"] == ["ORD-甲-1"]
        assert experiment["basis_complete"] is True

        ledger.finance_recalc("ORD-甲-1", include=True)
        report = ledger.daily_report("2026-11-24")
        experiment = next(item for item in report["experiments"] if item["round_no"] == 1)
        assert experiment["numerator"] == 1
        assert experiment["correction_order_ids"] == ["ORD-甲-1"]
        assert ledger.counts()["overlap"] == 0

        # 再切回结案侧，快照与现算口径重新一致，且不重复计数
        ledger.transfer_claim("ORD-甲-1", ClaimOwner.EXPERIMENT.value)
        assert ledger.batches[1].recompute()["numerator"] == 1
        assert ledger.counts() == {
            "experiment_orders": 1, "finance_orders": 0,
            "total_claimed": 1, "overlap": 0,
        }

    def test_finance_can_only_settle_handed_off_orders(self, store_ledger):
        ledger = store_ledger
        ledger.register_conversion(
            order_id="ORD-甲-1", user_id="甲",
            ordered_at=dt("14:58:00"), reported_at=dt("14:59:00"),
        )
        ledger.close_round(1, dt("15:05:00"))
        with pytest.raises(ValueError):
            ledger.finance_recalc("ORD-甲-1", include=True)

        batch = ledger.batches[1]
        batch.transfer_claim("ORD-甲-1", ClaimOwner.FINANCE.value)
        claim = ledger.finance_recalc("ORD-甲-1", include=True)
        assert claim.disposition == ConversionDisposition.FINANCE_CONFIRMED
        # 已计入结案的快照没有被财务复算改写
        assert ledger.batches[1].close_snapshot["numerator"] == 1
        assert ledger.counts()["overlap"] == 0

    def test_concurrent_close_and_late_report(self):
        for _ in range(20):
            ledger = ExperimentClosureLedger(
                "EXP_STORE",
                rounds=[ExperimentRound(1, dt("14:00:00"), dt("15:00:00"))],
            )
            ledger.enroll("甲", dt("14:50:00"))
            ledger.enroll("乙", dt("15:03:00"))
            barrier = threading.Barrier(2)

            def close_side():
                barrier.wait()
                ledger.close_round(1, dt("15:05:00"))

            def report_side():
                barrier.wait()
                ledger.register_conversion(
                    order_id="ORD-甲-1", user_id="甲",
                    ordered_at=dt("14:58:00"), reported_at=dt("15:05:00"),
                )

            threads = [
                threading.Thread(target=close_side),
                threading.Thread(target=report_side),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            # 无论哪边先到，甲只有一种处置，且不会被重复计数
            state = ledger.claim_state("ORD-甲-1")
            assert ledger.counts()["overlap"] == 0
            assert state["duplicate_submissions"] == 0
            if state["counted_in_closure"]:
                assert ledger.batches[1].close_snapshot["numerator"] == 1
                assert state["in_open_diff"] is False
            else:
                assert state["disposition"] == ConversionDisposition.DEFERRED.value
                assert ledger.batches[1].close_snapshot["numerator"] == 0
                # 日报此时不出数，也不把乙算进旧实验
                report = ledger.daily_report("2026-11-24")
                assert all(
                    item["round_no"] != 1 for item in report["experiments"]
                )
                ledger.handoff_unresolved()
                ledger.finance_recalc("ORD-甲-1", include=True)
                report = ledger.daily_report("2026-11-24")
                experiment = next(
                    item for item in report["experiments"] if item["round_no"] == 1
                )
                assert experiment["denominator"] == 1
                assert experiment["enrolled_user_ids"] == ["甲"]
