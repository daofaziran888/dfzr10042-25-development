"""指标对比领域服务：从有效转化批次取数，而不是各算各的。

对比方案的基准值/当前值/变化幅度全部由关联批次的「入组人数 / 有效购买数」
重算；没有关联批次时保留通用状态流转。
"""
from __future__ import annotations

from typing import Any

from app.experiment_closure import build_metric_comparison
from app.services.base import BaseModuleService
from app.services.valid_conversion_batch import LEDGERS
from app.store import store


class MetricCompareService(BaseModuleService):
    MODULE = "metric_compare"
    ENTITY = "对比方案"
    OPERATION_TERM = "计算"
    LIST_FIELDS = ["方案编号", "主指标", "对比指标", "对比周期", "基准值", "当前值", "变化幅度", "对比状态"]
    REQUIRED_FIELDS = ["方案编号", "主指标", "对比指标"]
    STATUS_ORDER = ["待计算", "计算中", "已完成", "数据缺失"]
    ACTION_RULES = {"启动计算": "计算中", "重算对比": "已完成", "标记缺失": "数据缺失"}
    NEGATIVE_ACTIONS = []

    def refresh_from_batch(self, entry: dict[str, Any]) -> dict[str, Any] | None:
        """按关联批次重算分母与购买，写回对比方案。"""
        batch_id = str(entry.get("关联批次") or "").strip()
        if not batch_id:
            return None
        for ledger in LEDGERS.values():
            for round_no, batch in ledger.batches.items():
                if batch.batch_id != batch_id:
                    continue
                comparison = build_metric_comparison(batch)
                entry.update({
                    "主指标": "入组人数",
                    "对比指标": "有效购买数",
                    "对比周期": f"第{round_no}轮",
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
                return entry
        return None

    def run_action(
        self, entry_id: int, action: str, remark: str | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"
        if action not in self.ACTION_RULES:
            return None, f"动作「{action}」不属于{self.MODULE}可执行范围"

        entry["status"] = self.ACTION_RULES[action]
        entry["pending"] = entry["status"] != self.STATUS_ORDER[-1]
        entry["abnormal"] = action in self.NEGATIVE_ACTIONS

        if action in ("启动计算", "重算对比"):
            refreshed = self.refresh_from_batch(entry)
            if refreshed is not None:
                return entry, (
                    f"{self.ENTITY}已按批次 {entry['关联批次']} 重算："
                    f"入组 {entry['基准值']} 人、有效购买 {entry['变化幅度']} 笔"
                )
        return entry, f"{self.ENTITY}已{action}"
