"""A/B实验领域服务：在通用状态流转之上接入有效转化批次结案。

「终止实验」= 停收后结案：除了把实验状态改为已结束，还要冻结有效转化批次，
让停收前已入组用户的迟到转化进入补审，而不是丢失或回写结案快照。
"""
from __future__ import annotations

from typing import Any

from app.experiment_closure import parse_datetime
from app.services.base import BaseModuleService
from app.services.valid_conversion_batch import (
    ValidConversionBatchService,
    ledger_for,
)
from app.store import store


def _optional_datetime(remark: str | None):
    """remark 里可以带结案时刻；解析不了就当未指定，交给账本默认值。"""
    if not remark:
        return None
    try:
        return parse_datetime(remark)
    except ValueError:
        return None


class AbExperimentService(BaseModuleService):
    MODULE = "ab_experiment"
    ENTITY = "实验方案"
    OPERATION_TERM = "归档"
    LIST_FIELDS = ["方案编号", "实验名称", "实验分组", "分流比例", "评估指标", "最小样本", "开始时间", "实验状态"]
    REQUIRED_FIELDS = ["方案编号", "实验名称", "实验分组"]
    STATUS_ORDER = ["设计期", "运行中", "已结束", "已终止"]
    ACTION_RULES = {"启动实验": "运行中", "终止实验": "已结束", "归档结果": "已终止"}
    NEGATIVE_ACTIONS = []

    @staticmethod
    def experiment_key(entry: dict[str, Any]) -> str:
        """实验在结案账本中的键：优先方案编号，回退记录 id。"""
        return str(entry.get("方案编号") or f"ab_experiment:{entry.get('id')}")

    def run_action(
        self, entry_id: int, action: str, remark: str | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"
        if action not in self.ACTION_RULES:
            return None, f"动作「{action}」不属于{self.MODULE}可执行范围"

        experiment_id = self.experiment_key(entry)
        batches = ValidConversionBatchService()
        message = f"{self.ENTITY}已{action}"

        try:
            if action == "终止实验":
                ledger = ledger_for(experiment_id)
                if ledger.rounds:
                    snapshot = ledger.close_experiment(_optional_datetime(remark))
                    message = (
                        f"{self.ENTITY}已终止并结案第 {snapshot['round_no']} 轮："
                        f"入组 {snapshot['denominator']} 人、有效购买 {snapshot['numerator']} 笔"
                    )
                else:
                    message = f"{self.ENTITY}已终止（尚未配置有效转化批次）"
            batches.persist(experiment_id)
        except (KeyError, ValueError) as error:
            return None, str(error)

        ledger = ledger_for(experiment_id)
        if action == "终止实验" and not ledger.rounds:
            # 没有批次可结案时保留显式结案状态，避免被空账本覆盖。
            entry["结案状态"] = "已结案"
        if action == "归档结果" and entry.get("结案状态") == "补审中":
            entry["结案状态"] = "已更正"

        entry["status"] = self.ACTION_RULES[action]
        entry["pending"] = entry["status"] != self.STATUS_ORDER[-1]
        entry["abnormal"] = action in self.NEGATIVE_ACTIONS
        return entry, message

    def domain_snapshot(self) -> dict[str, Any]:
        snapshot = super().domain_snapshot()
        snapshot["closure_states"] = {
            state: sum(1 for row in store.rows(self.MODULE) if row.get("结案状态") == state)
            for state in ["收集中", "已结案", "补审中", "已更正"]
        }
        return snapshot
