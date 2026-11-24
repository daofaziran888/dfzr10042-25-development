"""有效转化批次领域服务。

把 :mod:`app.experiment_closure` 的账本能力接入平台的通用资源模块：
同一实验下的所有轮次批次统一登记在服务层，批次行同步到内存仓库，供
列表、看板和前端工作台读取。

指标对比从这里取数：分母是批次内停收前完成入组的人数，分子是这些用户
在轮次内、入组之后、停收之前下单且通过原订单幂等校验的购买。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from app.experiment_closure import (
    BatchStatus,
    ConversionDisposition,
    ExperimentClosureLedger,
    build_daily_report,
    build_metric_comparison,
    parse_datetime,
)
from app.services.base import BaseModuleService
from app.store import store


# 实验账本注册表：experiment_id -> 账本。服务是进程内单例，账本随服务常驻。
LEDGERS: dict[str, ExperimentClosureLedger] = {}


def ledger_for(experiment_id: str) -> ExperimentClosureLedger:
    ledger = LEDGERS.get(experiment_id)
    if ledger is None:
        ledger = ExperimentClosureLedger(experiment_id)
        LEDGERS[experiment_id] = ledger
    return ledger


def _experiment_row(experiment_id: str) -> dict[str, Any] | None:
    """按方案编号或记录 id 找到实验行，保证接口和列表操作指向同一条记录。"""
    rows = store.rows("ab_experiment")
    for row in rows:
        if str(row.get("方案编号") or "") == experiment_id:
            return row
        if experiment_id == f"ab_experiment:{row.get('id')}":
            return row
    return None


def _normalize_orders(remark: str | None) -> list[str]:
    """remark 里可写多个原订单号，支持中英文逗号分隔。"""
    return [
        item.strip()
        for item in str(remark or "").replace("，", ",").split(",")
        if item.strip()
    ]


class ValidConversionBatchService(BaseModuleService):
    MODULE = "valid_conversion_batch"
    ENTITY = "有效转化批次"
    OPERATION_TERM = "结案"
    LIST_FIELDS = [
        "批次编号", "关联实验", "实验轮次", "停收时间",
        "入组人数", "有效购买数", "转化率", "未处置差异", "依据状态", "批次状态",
    ]
    REQUIRED_FIELDS = ["批次编号", "关联实验"]
    STATUS_ORDER = [status.value for status in BatchStatus]
    ACTIONS = ["结案", "补审", "移交财务", "财务复算"]
    ACTION_RULES = {
        "结案": BatchStatus.CLOSED.value,
        "补审": BatchStatus.REAUDIT.value,
        "移交财务": BatchStatus.REAUDIT.value,
        "财务复算": BatchStatus.CORRECTED.value,
    }
    NEGATIVE_ACTIONS = ["补审"]

    # ---------- 账本操作 ----------

    def configure_round(
        self,
        experiment_id: str,
        round_no: int,
        start_at: str | datetime,
        cutoff_at: str | datetime,
        *,
        round_duration_minutes: int = 60,
        auto_next_round: bool = True,
    ) -> dict[str, Any]:
        ledger = ledger_for(experiment_id)
        ledger.round_duration = timedelta(minutes=round_duration_minutes)
        ledger.auto_next_round = auto_next_round
        ledger.configure_round(round_no, start_at, cutoff_at)
        self._persist(experiment_id)
        return ledger.to_dict()

    def enroll(self, experiment_id: str, user_id: str, enrolled_at: str | datetime) -> dict[str, Any]:
        record = ledger_for(experiment_id).enroll(user_id, enrolled_at)
        self._persist(experiment_id)
        return record.to_dict()

    def report_conversion(
        self,
        experiment_id: str,
        *,
        order_id: str,
        user_id: str,
        ordered_at: str | datetime,
        reported_at: str | datetime | None = None,
        source: str = "late",
        round_no: int | None = None,
    ) -> dict[str, Any]:
        batch, claim, duplicate = ledger_for(experiment_id).register_conversion(
            order_id=order_id,
            user_id=user_id,
            ordered_at=ordered_at,
            reported_at=reported_at,
            source=source,
            round_no=round_no,
        )
        self._persist(experiment_id)
        return {
            "batch_id": batch.batch_id,
            "round_no": batch.round_no,
            "duplicate": duplicate,
            "claim": claim.to_dict(),
            "batch": batch.to_dict(),
        }

    def close(
        self, experiment_id: str, closed_at: str | datetime | None = None,
    ) -> dict[str, Any]:
        ledger = ledger_for(experiment_id)
        snapshot = ledger.close_experiment(closed_at)
        self._persist(experiment_id)
        return snapshot

    def close_round(
        self, experiment_id: str, round_no: int, closed_at: str | datetime | None = None,
    ) -> dict[str, Any]:
        ledger = ledger_for(experiment_id)
        snapshot = ledger.close_round(round_no, closed_at)
        self._persist(experiment_id)
        return snapshot

    def reaudit(self, experiment_id: str) -> dict[str, Any]:
        ledger = ledger_for(experiment_id)
        claims = ledger.reaudit_claims()
        self._persist(experiment_id)
        return {
            "experiment_id": experiment_id,
            "open_diff": [claim.to_dict() for claim in claims],
            "counts": ledger.counts(),
        }

    def handoff(self, experiment_id: str) -> dict[str, Any]:
        ledger = ledger_for(experiment_id)
        moved = ledger.handoff_unresolved()
        self._persist(experiment_id)
        return {
            "experiment_id": experiment_id,
            "handed_off": [claim.to_dict() for claim in moved],
            "counts": ledger.counts(),
        }

    def finance_recalc(
        self, experiment_id: str, order_id: str, *, include: bool = True,
    ) -> dict[str, Any]:
        ledger = ledger_for(experiment_id)
        claim = ledger.finance_recalc(order_id, include=include)
        self._persist(experiment_id)
        return {
            "claim": claim.to_dict(),
            "state": ledger.claim_state(order_id),
            "counts": ledger.counts(),
        }

    def transfer(self, experiment_id: str, order_id: str, owner: str) -> dict[str, Any]:
        ledger = ledger_for(experiment_id)
        state = ledger.transfer_claim(order_id, owner)
        self._persist(experiment_id)
        return state

    def metric_comparison(
        self, experiment_id: str, round_no: int | None = None,
    ) -> dict[str, Any]:
        """默认口径：最近一个已结案轮次；没有结案轮次时取最早一个未结案轮次。

        停收后自动生成的下一轮只是承接溢出入组，不能抢在旧轮次前面成为默认口径。
        """
        ledger = ledger_for(experiment_id)
        if round_no is None:
            closed = [
                rnd.round_no for rnd in ledger.rounds
                if ledger.batches.get(rnd.round_no)
                and ledger.batches[rnd.round_no].close_snapshot is not None
            ]
            if closed:
                round_no = max(closed)
            elif ledger.rounds:
                round_no = min(rnd.round_no for rnd in ledger.rounds)
            else:
                raise ValueError(f"实验 {experiment_id} 尚未配置轮次")
        batch = ledger.batches.get(round_no)
        if batch is None:
            raise KeyError(f"实验 {experiment_id} 不存在第 {round_no} 轮")
        return build_metric_comparison(batch)

    def _sync_metric_compare(self, experiment_id: str) -> None:
        """已存在的对比方案随批次口径刷新；未创建时不擅自新增。"""
        ledger = LEDGERS.get(experiment_id)
        if ledger is None:
            return
        rows = store.rows("metric_compare")
        for row in rows:
            batch_id = str(row.get("关联批次") or "")
            if not batch_id.startswith(f"VCB_{experiment_id}_"):
                continue
            for batch in ledger.batches.values():
                if batch.batch_id != batch_id:
                    continue
                comparison = build_metric_comparison(batch)
                row.update({
                    "基准值": comparison["入组人数"],
                    "当前值": comparison["转化率"],
                    "变化幅度": comparison["有效购买数"],
                    "对比状态": comparison["对比状态"],
                    "status": comparison["对比状态"],
                    "pending": comparison["对比状态"] != "已完成",
                    "abnormal": comparison["对比状态"] != "已完成",
                    "分母用户": ", ".join(comparison["分母用户"]),
                    "分子订单": ", ".join(comparison["分子订单"]),
                })

    def claim_state(self, experiment_id: str, order_id: str) -> dict[str, Any]:
        return ledger_for(experiment_id).claim_state(order_id)

    def materialize_metric_compare(
        self, experiment_id: str, round_no: int | None = None,
    ) -> dict[str, Any]:
        """把批次口径写成一条指标对比方案，供指标对比模块直接读取。

        主指标 = 入组人数（分母），对比指标 = 有效购买数（分子），
        当前值 = 转化率。依据不完整时对比状态标记为「数据缺失」。
        """
        comparison = self.metric_comparison(experiment_id, round_no)
        rows = store.rows("metric_compare")
        plan_id = f"MC_{comparison['batch_id']}"
        row = next((item for item in rows if item.get("方案编号") == plan_id), None)
        if row is None:
            row = {
                "id": max((int(item.get("id", 0)) for item in rows), default=0) + 1,
                "方案编号": plan_id,
                "pending": True,
                "abnormal": False,
            }
            rows.append(row)
        row.update({
            "主指标": "入组人数",
            "对比指标": "有效购买数",
            "对比周期": f"第{comparison['round_no']}轮",
            "基准值": comparison["入组人数"],
            "当前值": comparison["转化率"],
            "变化幅度": comparison["有效购买数"],
            "对比状态": comparison["对比状态"],
            "关联批次": comparison["batch_id"],
            "分母用户": ", ".join(comparison["分母用户"]),
            "分子订单": ", ".join(comparison["分子订单"]),
            "status": comparison["对比状态"],
            "abnormal": comparison["对比状态"] != "已完成",
        })
        return row

    def comparison_payload(
        self, experiment_id: str, round_no: int | None = None,
    ) -> dict[str, Any]:
        """指标对比数据面：同时给出纯计算结果和已写入的对比方案行。"""
        comparison = self.metric_comparison(experiment_id, round_no)
        return {
            "comparison": comparison,
            "metric_compare_row": self.materialize_metric_compare(experiment_id, round_no),
        }

    def daily_report(
        self,
        experiment_id: str | None = None,
        report_date: str | None = None,
    ) -> dict[str, Any]:
        if experiment_id is not None:
            return ledger_for(experiment_id).daily_report(report_date)
        return build_daily_report(list(LEDGERS.values()), report_date)

    def snapshot(self, experiment_id: str) -> dict[str, Any]:
        return ledger_for(experiment_id).to_dict()

    # ---------- 实验行联动 ----------

    def _sync_experiment_row(
        self, experiment_id: str, *, closure_state: str | None = None,
    ) -> None:
        """把账本口径写回实验行：结案状态、当次结案订单、待更正订单互斥登记。"""
        ledger = LEDGERS.get(experiment_id)
        row = _experiment_row(experiment_id)
        if ledger is None or row is None:
            return
        closed_rounds = [
            round_no for round_no, batch in ledger.batches.items()
            if batch.close_snapshot is not None
        ]
        if closed_rounds:
            target_round = max(closed_rounds)
        elif ledger.batches:
            target_round = max(ledger.batches)
        else:
            target_round = None
        batch = ledger.batches.get(target_round) if target_round is not None else None
        row["结案状态"] = closure_state or (batch.status.value if batch else "收集中")
        row["结案轮次"] = target_round
        row["依据完整"] = bool(batch.basis_complete) if batch else False
        closure_orders: list[str] = []
        correction_orders: list[str] = []
        reaudit_orders: list[str] = []
        # 只统计本次结案那一轮的原订单，避免把其他轮次的订单混进实验行。
        for claim in (batch.claims.values() if batch else []):
            if claim.counted_in_closure:
                closure_orders.append(claim.order_id)
            elif claim.counted_in_correction:
                correction_orders.append(claim.order_id)
            elif claim.disposition == ConversionDisposition.DEFERRED:
                reaudit_orders.append(claim.order_id)
        row["当次结案订单"] = ", ".join(sorted(set(closure_orders)))
        row["待更正订单"] = ", ".join(sorted(set(reaudit_orders)))
        row["已更正订单"] = ", ".join(sorted(set(correction_orders)))
        if batch is not None:
            metrics = batch.recompute()
            row["入组人数"] = metrics["denominator"]
            row["有效购买数"] = metrics["numerator"]
            row["未处置差异"] = len(batch.unresolved)

    # ---------- 同步到通用模块 ----------

    def sync(self, experiment_id: str) -> None:
        """把账本中各轮批次写成一行记录，供列表和看板读取。"""
        ledger = LEDGERS.get(experiment_id)
        if ledger is None:
            return
        rows = store.rows(self.MODULE)
        by_batch = {row.get("批次编号"): row for row in rows}
        for round_no in sorted(ledger.batches):
            batch = ledger.batches[round_no]
            metrics = batch.recompute()
            row = by_batch.get(batch.batch_id)
            if row is None:
                row = {
                    "id": max((int(item.get("id", 0)) for item in rows), default=0) + 1,
                    "批次编号": batch.batch_id,
                }
                rows.append(row)
            row.update({
                "关联实验": experiment_id,
                "实验轮次": round_no,
                "停收时间": metrics["cutoff_at"],
                "入组人数": metrics["denominator"],
                "有效购买数": metrics["numerator"],
                "转化率": metrics["conversion_rate"],
                "未处置差异": len(batch.unresolved),
                "依据状态": "依据完整" if batch.basis_complete else "依据不完整",
                "批次状态": batch.status.value,
                "status": batch.status.value,
                "pending": batch.status != BatchStatus.CORRECTED,
                "abnormal": not batch.basis_complete,
            })

    def _persist(self, experiment_id: str) -> None:
        """统一出口：批次行、实验行、对比行一次同步。"""
        self.sync(experiment_id)
        self._sync_experiment_row(experiment_id)
        self._sync_metric_compare(experiment_id)

    def persist(self, experiment_id: str) -> None:
        """供实验服务的状态流转复用。"""
        self._persist(experiment_id)

    # ---------- 行级动作 ----------

    def _experiment_of(self, entry: dict[str, Any]) -> str:
        experiment_id = str(entry.get("关联实验") or "").strip()
        if not experiment_id:
            raise ValueError(f"批次 {entry.get('批次编号')} 未关联实验")
        return experiment_id

    # ---------- 登记与动作 ----------

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        """工作台登记一条批次配置：等价于声明一轮实验的停收时间窗。

        批次编号可省略，由账本按「实验 + 轮次」生成；停收时间是必填硬边界。
        """
        experiment_id = str(values.get("关联实验") or "").strip()
        cutoff_at = str(values.get("停收时间") or "").strip()
        missing = [
            label for label, value in (("关联实验", experiment_id), ("停收时间", cutoff_at))
            if not value
        ]
        if missing:
            return None, missing

        round_no = int(values.get("实验轮次") or 1)
        start_at = str(values.get("开始时间") or "").strip()
        if not start_at:
            cutoff = parse_datetime(cutoff_at)
            assert cutoff is not None
            start_at = (cutoff - timedelta(hours=1)).isoformat()

        try:
            self.configure_round(experiment_id, round_no, start_at, cutoff_at)
        except ValueError as error:
            return None, [str(error)]

        batch_id = str(values.get("批次编号") or f"VCB_{experiment_id}_R{round_no}")
        row = next(
            (item for item in store.rows(self.MODULE) if item.get("批次编号") == batch_id),
            None,
        )
        return row, []

    def run_action(
        self, entry_id: int, action: str, remark: str | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"
        if action not in self.ACTIONS:
            return None, f"动作「{action}」不属于{self.MODULE}可执行范围"

        try:
            experiment_id = self._experiment_of(entry)
            if action == "结案":
                snapshot = self.close(experiment_id)
                message = (
                    f"实验 {experiment_id} 已结案：入组 {snapshot['denominator']} 人、"
                    f"有效购买 {snapshot['numerator']} 笔"
                )
            elif action == "补审":
                result = self.reaudit(experiment_id)
                message = f"实验 {experiment_id} 待处置差异 {len(result['open_diff'])} 笔"
            elif action == "移交财务":
                result = self.handoff(experiment_id)
                message = f"实验 {experiment_id} 已移交财务 {len(result['handed_off'])} 笔"
            else:  # 财务复算
                order_ids = _normalize_orders(remark)
                if not order_ids:
                    # 未指定原订单时，处置本实验所有「留作更正」的差异。
                    order_ids = sorted({
                        claim.order_id
                        for ledger_batch in ledger_for(experiment_id).batches.values()
                        for claim in ledger_batch.claims.values()
                        if claim.disposition == ConversionDisposition.DEFERRED
                    })
                if not order_ids:
                    return entry, "当前没有待财务复算的原订单"
                for order_id in order_ids:
                    self.finance_recalc(experiment_id, order_id, include=True)
                message = f"实验 {experiment_id} 已财务复算 {len(order_ids)} 笔"
        except (KeyError, ValueError) as error:
            return entry, str(error)

        self._persist(experiment_id)
        updated = store.find(self.MODULE, entry_id) or entry
        return updated, message

    def domain_snapshot(self) -> dict[str, Any]:
        snapshot = super().domain_snapshot()
        snapshot["experiments"] = sorted(LEDGERS)
        snapshot["open_diffs"] = sum(
            len(batch.unresolved)
            for ledger in LEDGERS.values()
            for batch in ledger.batches.values()
        )
        return snapshot
