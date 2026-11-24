"""实验结案 · 有效转化批次领域引擎。

问题背景
--------
实验「终止 / 结案」时，停收前已经入组的用户仍可能在停收前下单，但订单回传
（尤其是迟到回传）可能晚于负责人点击结案的时刻。如果只看结案那一瞬间的数据，
就会出现：

* 停收前真实发生的购买没有进入指标对比；
* 结案与迟到回传并发时，两边互相覆盖，日报口径不稳定；
* 停收后才入组的用户（例如 15:03 才进组的乙）被误算进旧实验；
* 同一笔原订单被结案侧和财务更正侧重复领用。

本模块用显式的「有效转化批次」（:class:`ValidConversionBatch`）承载一轮实验的
有效转化。所有指标都从批次里的入组记录和订单认领状态**重算**：

1. 分母 = 该轮次内、停收时点前完成入组的用户；
2. 分子 = 上述用户在该轮次内、入组之后、停收时点前下单的订单，每笔原订单只记一次；
3. 轮次归属取入组时实际落入的轮次，而不是用订单回传时间重新推断；
4. 结案侧（experiment）与财务更正侧（finance）对同一笔订单互斥领用，来回切换
   只改变归属，不会重复计数；
5. 日报只消费「依据完整」的实验结果；存在未处置差异时宁可跳过，也不出半截数。

所有时间都是 naive ``datetime``；输入既可以是 ``datetime`` 也可以是 ISO 字符串。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from threading import RLock
from typing import Any, Iterable


# ============================================================
# 枚举
# ============================================================

class BatchStatus(str, Enum):
    """有效转化批次状态。"""

    COLLECTING = "收集中"   # 停收前/结案前，允许继续登记转化
    CLOSED = "已结案"       # 已生成结案快照
    REAUDIT = "补审中"      # 结案后出现迟到回传，差异尚未处置完
    CORRECTED = "已更正"    # 差异已由财务复算处置


class ConversionDisposition(str, Enum):
    """单笔订单在结案/更正口径中的处置状态。"""

    PENDING = "待结案"               # 已登记、等待本次结案计入
    IN_CLOSURE = "已计入结案"        # 已进入当次结案口径
    DEFERRED = "留作更正"            # 结案后回传，等待财务复算
    FINANCE_CONFIRMED = "财务已复算"  # 财务已完成更正复算
    REJECTED = "不予计入"            # 时点/轮次/归属校验不通过


class ClaimOwner(str, Enum):
    """订单归属方：结案侧或财务更正侧，同一时刻只能属于一边。"""

    EXPERIMENT = "experiment"
    FINANCE = "finance"


# ============================================================
# 工具
# ============================================================

def parse_datetime(value: datetime | str | None) -> datetime | None:
    """把 ISO 字符串或 ``datetime`` 统一成 naive ``datetime``。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo else value
    text = str(value).strip()
    if not text:
        return None
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


# ============================================================
# 轮次与入组
# ============================================================

@dataclass(frozen=True)
class ExperimentRound:
    """实验的一轮投放：停收时间（cutoff）是分子/分母的硬边界。"""

    round_no: int
    start_at: datetime
    cutoff_at: datetime

    def contains(self, at: datetime) -> bool:
        return self.start_at <= at <= self.cutoff_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "round_no": self.round_no,
            "start_at": _iso(self.start_at),
            "cutoff_at": _iso(self.cutoff_at),
        }


@dataclass
class EnrollmentRecord:
    """用户的入组记录；``round_no`` 是入组时实际归入的轮次，后续不再改写。"""

    experiment_id: str
    user_id: str
    round_no: int
    enrolled_at: datetime
    cutoff_at: datetime

    @property
    def enrolled_before_cutoff(self) -> bool:
        return self.enrolled_at <= self.cutoff_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "experiment_id": self.experiment_id,
            "user_id": self.user_id,
            "round_no": self.round_no,
            "enrolled_at": _iso(self.enrolled_at),
            "cutoff_at": _iso(self.cutoff_at),
            "enrolled_before_cutoff": self.enrolled_before_cutoff,
        }


# ============================================================
# 订单认领
# ============================================================

@dataclass
class ConversionClaim:
    """一笔原订单在某个实验轮次中的认领记录。

    ``order_id`` 是原订单号，也是幂等键：重复提交不会产生新的认领，只会累加
    ``duplicate_submissions``。``round_no`` 取入组记录上的实际轮次。
    """

    order_id: str
    experiment_id: str
    user_id: str
    ordered_at: datetime
    reported_at: datetime
    round_no: int | None = None
    valid: bool = False
    reason: str = ""
    disposition: ConversionDisposition = ConversionDisposition.PENDING
    owner: str | None = None
    closure_locked: bool = False
    duplicate_submissions: int = 0
    source: str = "late"

    @property
    def counted_in_closure(self) -> bool:
        """当前是否计入实验结案口径。"""
        return (
            self.valid
            and self.disposition == ConversionDisposition.IN_CLOSURE
            and self.owner == ClaimOwner.EXPERIMENT.value
        )

    @property
    def counted_in_correction(self) -> bool:
        """当前是否计入财务更正口径。"""
        return (
            self.valid
            and self.disposition == ConversionDisposition.FINANCE_CONFIRMED
            and self.owner == ClaimOwner.FINANCE.value
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "original_order_id": self.order_id,
            "order_id": self.order_id,
            "experiment_id": self.experiment_id,
            "user_id": self.user_id,
            "ordered_at": _iso(self.ordered_at),
            "reported_at": _iso(self.reported_at),
            "round_no": self.round_no,
            "valid": self.valid,
            "reason": self.reason,
            "disposition": self.disposition.value,
            "owner": self.owner,
            "closure_locked": self.closure_locked,
            "counted_in_closure": self.counted_in_closure,
            "counted_in_correction": self.counted_in_correction,
            "duplicate_submissions": self.duplicate_submissions,
            "source": self.source,
        }


# ============================================================
# 有效转化批次
# ============================================================

@dataclass
class ValidConversionBatch:
    """一轮实验的有效转化批次：入组人数是分母，有效订单是分子。"""

    experiment_id: str
    round_no: int
    cutoff_at: datetime
    batch_id: str = ""
    opened_at: datetime | None = None
    status: BatchStatus = BatchStatus.COLLECTING
    basis_complete: bool = True
    enrollments: dict[str, EnrollmentRecord] = field(default_factory=dict)
    claims: dict[str, ConversionClaim] = field(default_factory=dict)
    unresolved: set[str] = field(default_factory=set)
    close_snapshot: dict[str, Any] | None = None
    revision: int = 0
    history: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.cutoff_at = parse_datetime(self.cutoff_at)  # type: ignore[assignment]
        self.opened_at = parse_datetime(self.opened_at)  # type: ignore[assignment]
        if not self.batch_id:
            self.batch_id = f"VCB_{self.experiment_id}_R{self.round_no}"
        if self.opened_at is None:
            self.opened_at = self.cutoff_at

    # ---------- 入组 ----------

    def enroll(self, user_id: str, enrolled_at: datetime | str) -> EnrollmentRecord:
        """登记入组；同一用户重复入组保持第一次记录（幂等）。"""
        enrolled_at = parse_datetime(enrolled_at)
        assert enrolled_at is not None
        if user_id in self.enrollments:
            return self.enrollments[user_id]
        if enrolled_at > self.cutoff_at:
            raise ValueError(
                f"用户 {user_id} 入组时间晚于本批停收时间，不能计入第 {self.round_no} 轮"
            )
        record = EnrollmentRecord(
            experiment_id=self.experiment_id,
            user_id=user_id,
            round_no=self.round_no,
            enrolled_at=enrolled_at,
            cutoff_at=self.cutoff_at,
        )
        self.enrollments[user_id] = record
        self._log("enroll", {"user_id": user_id, "enrolled_at": _iso(enrolled_at)})
        return record

    # ---------- 订单 ----------

    def register_conversion(
        self,
        *,
        order_id: str,
        user_id: str,
        ordered_at: datetime | str,
        reported_at: datetime | str | None = None,
        source: str = "late",
    ) -> tuple[ConversionClaim, bool]:
        """登记一笔订单，返回 ``(claim, is_duplicate)``。

        校验链：原订单幂等 → 用户在本批停收前入组 → 下单不早于入组 →
        下单不晚于停收。结案后才回传的有效订单进入补审（留作更正），
        不回写已冻结的结案快照。
        """
        ordered_at = parse_datetime(ordered_at)
        reported_at = parse_datetime(reported_at) or ordered_at
        assert ordered_at is not None and reported_at is not None

        existing = self.claims.get(order_id)
        if existing is not None:
            existing.duplicate_submissions += 1
            self._log("duplicate_order", {
                "order_id": order_id,
                "duplicate_submissions": existing.duplicate_submissions,
            })
            return existing, True

        claim = ConversionClaim(
            order_id=order_id,
            experiment_id=self.experiment_id,
            user_id=user_id,
            ordered_at=ordered_at,
            reported_at=reported_at,
            source=source,
        )
        enrollment = self.enrollments.get(user_id)

        if enrollment is None:
            claim.reason = "未在该轮次停收前入组"
            claim.disposition = ConversionDisposition.REJECTED
        elif enrollment.enrolled_at > self.cutoff_at:
            claim.reason = "入组晚于停收时间"
            claim.disposition = ConversionDisposition.REJECTED
        elif ordered_at < enrollment.enrolled_at:
            claim.reason = "下单早于入组时间"
            claim.disposition = ConversionDisposition.REJECTED
        elif ordered_at > self.cutoff_at:
            claim.reason = "下单晚于停收时间，不属于本实验轮次"
            claim.disposition = ConversionDisposition.REJECTED
        else:
            claim.valid = True
            claim.round_no = enrollment.round_no
            if self.status in (BatchStatus.CLOSED, BatchStatus.REAUDIT, BatchStatus.CORRECTED):
                # 结案之后才回传：进入补审，留作更正。
                claim.disposition = ConversionDisposition.DEFERRED
                claim.owner = None
                self.unresolved.add(order_id)
                self.status = BatchStatus.REAUDIT
                self.basis_complete = False
                self._log("late_after_close", {"order_id": order_id, "user_id": user_id})
            else:
                claim.disposition = ConversionDisposition.PENDING

        self.claims[order_id] = claim
        self._log("register_order", claim.to_dict())
        return claim, False

    # ---------- 重算 ----------

    def enrolled_user_ids(self) -> list[str]:
        """本批分母对应的那批人：本批次、停收前完成入组。"""
        return sorted(
            user_id for user_id, record in self.enrollments.items()
            if record.round_no == self.round_no and record.enrolled_before_cutoff
        )

    def snapshot_orders(self) -> list[ConversionClaim]:
        """进入本批结案口径的订单：有效、轮次一致、待结案或已计入结案。"""
        return [
            claim for claim in self.claims.values()
            if claim.valid
            and claim.round_no == self.round_no
            and claim.disposition in (
                ConversionDisposition.PENDING,
                ConversionDisposition.IN_CLOSURE,
            )
        ]

    def purchase_order_ids(self) -> list[str]:
        """当前计入结案口径的分子订单号。"""
        return sorted({claim.order_id for claim in self.snapshot_orders()})

    def recompute(self) -> dict[str, Any]:
        """重算分母与购买；轮次使用入组时实际归入的轮次，不重新推断。"""
        denominator_users = self.enrolled_user_ids()
        purchases = self.purchase_order_ids()
        denominator = len(denominator_users)
        numerator = len(purchases)
        return {
            "batch_id": self.batch_id,
            "experiment_id": self.experiment_id,
            "round_no": self.round_no,
            "cutoff_at": _iso(self.cutoff_at),
            "denominator": denominator,
            "numerator": numerator,
            "conversion_rate": round(numerator / denominator, 4) if denominator else 0.0,
            "enrolled_user_ids": denominator_users,
            "purchase_order_ids": purchases,
            "basis_complete": self.basis_complete,
            "revision": self.revision,
            "status": self.status.value,
        }

    # ---------- 结案 ----------

    def close(self, closed_at: datetime | str | None = None) -> dict[str, Any]:
        """冻结结案快照。重复结案返回同一份快照（幂等）。"""
        if self.close_snapshot is not None:
            return dict(self.close_snapshot)

        closed_at = parse_datetime(closed_at) or parse_datetime(self.cutoff_at)
        for claim in self.claims.values():
            if (
                claim.valid
                and claim.round_no == self.round_no
                and claim.disposition == ConversionDisposition.PENDING
            ):
                claim.disposition = ConversionDisposition.IN_CLOSURE
                claim.owner = ClaimOwner.EXPERIMENT.value
                # 锁定后既不能被财务再次领用，也不会被迟到回传回写。
                claim.closure_locked = True
        self.revision += 1
        snapshot = self.recompute()
        snapshot.update({
            "closed_at": _iso(closed_at),
            "status": BatchStatus.CLOSED.value,
            "unresolved_order_ids": sorted(self.unresolved),
        })
        self.basis_complete = not self.unresolved
        snapshot["basis_complete"] = self.basis_complete
        self.close_snapshot = snapshot
        self.status = BatchStatus.CLOSED
        self._log("close", {
            "closed_at": _iso(closed_at),
            "denominator": snapshot["denominator"],
            "numerator": snapshot["numerator"],
            "order_ids": snapshot["purchase_order_ids"],
        })
        return dict(snapshot)

    # ---------- 补审与财务 ----------

    def unresolved_claims(self) -> list[ConversionClaim]:
        return [
            self.claims[order_id] for order_id in sorted(self.unresolved)
            if order_id in self.claims and self.claims[order_id].valid
        ]

    def handoff_unresolved(self) -> list[ConversionClaim]:
        """把尚未处置的差异交给财务复算；差异离开实验侧，等待财务裁定。"""
        moved: list[ConversionClaim] = []
        for claim in self.unresolved_claims():
            claim.owner = ClaimOwner.FINANCE.value
            claim.disposition = ConversionDisposition.DEFERRED
            moved.append(claim)
        self.unresolved.clear()
        self.basis_complete = True
        if moved:
            self.status = BatchStatus.REAUDIT
            self._log("handoff_finance", {
                "order_ids": [claim.order_id for claim in moved],
            })
        return moved

    def settle_finance(self, order_id: str, *, include: bool = True) -> ConversionClaim:
        """财务复算：确认该差异是否计入本次更正。"""
        claim = self.claims.get(order_id)
        if claim is None:
            raise KeyError(f"订单 {order_id} 不在批次 {self.batch_id} 中")
        if not claim.valid:
            raise ValueError(f"订单 {order_id} 时点校验未通过，不能进入财务复算")
        if claim.owner != ClaimOwner.FINANCE.value:
            raise ValueError(
                f"订单 {order_id} 尚未移交财务（当前归属：{claim.owner or '无'}）"
            )
        if include:
            claim.disposition = ConversionDisposition.FINANCE_CONFIRMED
        else:
            claim.disposition = ConversionDisposition.REJECTED
            claim.reason = "财务裁定不计入本次更正"
            claim.owner = None
        self.revision += 1
        self._log("finance_recalc", {
            "order_id": order_id,
            "include": include,
            "owner": claim.owner,
        })
        if not any(
            c.disposition == ConversionDisposition.DEFERRED for c in self.claims.values()
        ):
            self.status = BatchStatus.CORRECTED
        return claim

    def transfer_claim(self, order_id: str, owner: str) -> ConversionClaim:
        """在结案侧与财务侧之间移动订单归属。

        同一笔订单只有一个 ``owner``，来回切换只改变归属，不会重复计数：

        * 已计入结案快照的订单切到财务侧后，历史快照保持不变，
          ``closure_locked`` 记住它进过当次结案，切回来可以恢复；
        * 从未进入结案快照的迟到订单不能再被追加进已冻结的结案，
          只能留在财务更正侧，避免结案口径被事后改写。
        """
        claim = self.claims.get(order_id)
        if claim is None:
            raise KeyError(f"订单 {order_id} 不在批次 {self.batch_id} 中")
        if not claim.valid:
            raise ValueError(f"订单 {order_id} 时点校验未通过，不能转移归属")
        owner = ClaimOwner(owner).value
        if claim.owner == owner:
            return claim  # 幂等

        if owner == ClaimOwner.FINANCE.value:
            if claim.disposition == ConversionDisposition.FINANCE_CONFIRMED:
                raise ValueError(f"订单 {order_id} 已完成财务复算，不能再移交")
            claim.disposition = ConversionDisposition.DEFERRED
            claim.owner = ClaimOwner.FINANCE.value
            self.unresolved.discard(order_id)
        else:
            if self.close_snapshot is not None and not claim.closure_locked:
                raise ValueError(
                    f"批次 {self.batch_id} 已结案，订单 {order_id} 不能追加入当次结案，"
                    "请走财务更正"
                )
            claim.owner = ClaimOwner.EXPERIMENT.value
            if claim.closure_locked:
                claim.disposition = ConversionDisposition.IN_CLOSURE
            elif self.close_snapshot is not None:
                claim.disposition = ConversionDisposition.IN_CLOSURE
            else:
                claim.disposition = ConversionDisposition.PENDING
            self.unresolved.discard(order_id)
            if not self.unresolved:
                self.basis_complete = True

        self.revision += 1
        self._log("transfer_claim", {"order_id": order_id, "owner": claim.owner})
        return claim

    # ---------- 输出 ----------

    def counts(self) -> dict[str, int]:
        """按归属方统计互斥的订单数，便于验证不会重复领用。"""
        experiment_orders = {
            claim.order_id for claim in self.claims.values() if claim.counted_in_closure
        }
        finance_orders = {
            claim.order_id for claim in self.claims.values() if claim.counted_in_correction
        }
        return {
            "experiment_orders": len(experiment_orders),
            "finance_orders": len(finance_orders),
            "total_claimed": len(experiment_orders | finance_orders),
            "overlap": len(experiment_orders & finance_orders),
        }

    def to_dict(self) -> dict[str, Any]:
        metrics = self.recompute()
        return {
            "batch_id": self.batch_id,
            "experiment_id": self.experiment_id,
            "round_no": self.round_no,
            "cutoff_at": _iso(self.cutoff_at),
            "status": self.status.value,
            "basis_complete": self.basis_complete,
            "revision": self.revision,
            "denominator": metrics["denominator"],
            "numerator": metrics["numerator"],
            "conversion_rate": metrics["conversion_rate"],
            "enrolled_user_ids": metrics["enrolled_user_ids"],
            "purchase_order_ids": metrics["purchase_order_ids"],
            "unresolved_order_ids": sorted(self.unresolved),
            "enrollments": [record.to_dict() for record in self.enrollments.values()],
            "claims": [claim.to_dict() for claim in self.claims.values()],
            "close_snapshot": dict(self.close_snapshot) if self.close_snapshot else None,
            "counts": self.counts(),
            "history": list(self.history),
        }

    def _log(self, event: str, payload: dict[str, Any]) -> None:
        self.history.append({"event": event, **payload})


# ============================================================
# 实验级账本
# ============================================================

class ExperimentClosureLedger:
    """一个实验的结案账本：管理各轮批次、并发登记、结案、补审和财务复算。"""

    def __init__(
        self,
        experiment_id: str,
        *,
        rounds: Iterable[ExperimentRound] | None = None,
        round_duration: timedelta = timedelta(hours=1),
        auto_next_round: bool = True,
        now_provider: Any = None,
    ) -> None:
        self.experiment_id = experiment_id
        self.round_duration = round_duration
        self.auto_next_round = auto_next_round
        self.now_provider = now_provider or datetime.now
        self.rounds: list[ExperimentRound] = list(rounds or [])
        self.batches: dict[int, ValidConversionBatch] = {}
        self.lock = RLock()
        self.history: list[dict[str, Any]] = []
        for rnd in self.rounds:
            self._ensure_batch(rnd)

    # ---------- 配置 ----------

    def configure_round(
        self,
        round_no: int,
        start_at: datetime | str,
        cutoff_at: datetime | str,
    ) -> ExperimentRound:
        """登记/覆盖一轮实验时间窗。"""
        start = parse_datetime(start_at)
        cutoff = parse_datetime(cutoff_at)
        assert start is not None and cutoff is not None
        if cutoff < start:
            raise ValueError("停收时间不能早于开始时间")
        rnd = ExperimentRound(round_no=round_no, start_at=start, cutoff_at=cutoff)
        with self.lock:
            self.rounds = [item for item in self.rounds if item.round_no != round_no]
            self.rounds.append(rnd)
            self.rounds.sort(key=lambda item: item.round_no)
            self._ensure_batch(rnd)
            self._log("configure_round", rnd.to_dict())
        return rnd

    def _ensure_batch(self, rnd: ExperimentRound) -> ValidConversionBatch:
        batch = self.batches.get(rnd.round_no)
        if batch is None:
            batch = ValidConversionBatch(
                experiment_id=self.experiment_id,
                round_no=rnd.round_no,
                cutoff_at=rnd.cutoff_at,
                opened_at=rnd.start_at,
            )
            self.batches[rnd.round_no] = batch
        return batch

    def _round_for(self, at: datetime) -> ExperimentRound | None:
        for rnd in self.rounds:
            if rnd.contains(at):
                return rnd
        return None

    def _next_round(self, at: datetime) -> ExperimentRound | None:
        if not self.auto_next_round or not self.rounds:
            return None
        last = max(self.rounds, key=lambda item: item.round_no)
        if at <= last.cutoff_at:
            return None
        start = last.cutoff_at
        while start + self.round_duration <= at:
            start += self.round_duration
        return ExperimentRound(
            round_no=last.round_no + 1,
            start_at=start,
            cutoff_at=start + self.round_duration,
        )

    def _batch_for_round(self, round_no: int) -> ValidConversionBatch:
        rnd = next((item for item in self.rounds if item.round_no == round_no), None)
        if rnd is None:
            raise KeyError(f"实验 {self.experiment_id} 不存在第 {round_no} 轮")
        return self._ensure_batch(rnd)

    def _find_claim(self, order_id: str) -> tuple[ValidConversionBatch, ConversionClaim] | None:
        for round_no in sorted(self.batches):
            batch = self.batches[round_no]
            if order_id in batch.claims:
                return batch, batch.claims[order_id]
        return None

    def _find_enrollment(self, user_id: str) -> EnrollmentRecord | None:
        for round_no in sorted(self.batches):
            record = self.batches[round_no].enrollments.get(user_id)
            if record is not None:
                return record
        return None

    # ---------- 入组与订单 ----------

    def enroll(self, user_id: str, enrolled_at: datetime | str) -> EnrollmentRecord:
        """按入组时点归入实际轮次；停收后的入组进入下一轮，不污染旧批次。"""
        at = parse_datetime(enrolled_at)
        assert at is not None
        with self.lock:
            rnd = self._round_for(at)
            if rnd is None:
                rnd = self._next_round(at)
                if rnd is None:
                    raise ValueError("入组时间不在任何实验轮次内")
                self.rounds.append(rnd)
                self.rounds.sort(key=lambda item: item.round_no)
            batch = self._ensure_batch(rnd)
            record = batch.enroll(user_id, at)
            self._log("enroll", {
                "user_id": user_id, "round_no": rnd.round_no, "enrolled_at": _iso(at),
            })
            return record

    def register_conversion(
        self,
        *,
        order_id: str,
        user_id: str,
        ordered_at: datetime | str,
        reported_at: datetime | str | None = None,
        source: str = "late",
        round_no: int | None = None,
    ) -> tuple[ValidConversionBatch, ConversionClaim, bool]:
        """登记订单（含迟到回传）。

        轮次以该用户入组时实际归入的轮次为准；原订单号全局幂等。
        返回 ``(batch, claim, is_duplicate)``。
        """
        ordered = parse_datetime(ordered_at)
        reported = parse_datetime(reported_at) or ordered
        assert ordered is not None and reported is not None
        with self.lock:
            existing = self._find_claim(order_id)
            if existing is not None:
                batch, claim = existing
                claim.duplicate_submissions += 1
                batch._log("duplicate_order", {
                    "order_id": order_id,
                    "duplicate_submissions": claim.duplicate_submissions,
                })
                return batch, claim, True

            enrollment = self._find_enrollment(user_id)
            if round_no is not None:
                batch = self._batch_for_round(round_no)
            elif enrollment is not None:
                batch = self._batch_for_round(enrollment.round_no)
            elif not self.batches:
                raise ValueError("实验尚未配置任何轮次")
            else:
                # 未入组用户仍记录在最早一批做审计，但不计入分子。
                batch = self._batch_for_round(min(self.batches))

            claim, _ = batch.register_conversion(
                order_id=order_id,
                user_id=user_id,
                ordered_at=ordered,
                reported_at=reported,
                source=source,
            )
            self._log("register_conversion", {
                "order_id": order_id,
                "user_id": user_id,
                "round_no": batch.round_no,
                "valid": claim.valid,
                "reason": claim.reason,
                "disposition": claim.disposition.value,
            })
            return batch, claim, False

    # ---------- 结案 ----------

    def close_round(
        self, round_no: int, closed_at: datetime | str | None = None,
    ) -> dict[str, Any]:
        with self.lock:
            batch = self._batch_for_round(round_no)
            snapshot = batch.close(parse_datetime(closed_at))
            self._log("close_round", {
                "round_no": round_no,
                "denominator": snapshot["denominator"],
                "numerator": snapshot["numerator"],
            })
            return snapshot

    def close_experiment(self, closed_at: datetime | str | None = None) -> dict[str, Any]:
        """结案「当前已停收的那一轮」，而不是盲目取最大轮次。

        例如 15:00 停收的门店实验，乙 15:03 才入组会被归入下一轮（16:00 停收）；
        15:05 点结案时目标仍是第 1 轮，不能把乙那一轮当成结案对象。未显式给出
        结案时刻时，以目标轮次的停收时间作为确定性默认值。
        """
        with self.lock:
            if not self.rounds:
                raise ValueError("实验尚未配置任何轮次")
            moment = parse_datetime(closed_at)
            if moment is not None:
                stopped = [rnd for rnd in self.rounds if rnd.cutoff_at <= moment]
                if stopped:
                    target = max(stopped, key=lambda item: item.round_no)
                else:
                    within = [rnd for rnd in self.rounds if rnd.contains(moment)]
                    target = within[-1] if within else min(
                        self.rounds, key=lambda item: item.round_no
                    )
            else:
                target = max(self.rounds, key=lambda item: item.round_no)
            if closed_at is None:
                closed_at = self.now_provider()
            return self.close_round(target.round_no, closed_at)

    # ---------- 补审 ----------

    def reaudit_claims(self) -> list[ConversionClaim]:
        """列出结案后回传、尚未处置的差异。"""
        with self.lock:
            return [
                claim for batch in self.batches.values()
                for claim in batch.unresolved_claims()
            ]

    def handoff_unresolved(self) -> list[ConversionClaim]:
        with self.lock:
            moved: list[ConversionClaim] = []
            for round_no in sorted(self.batches):
                moved.extend(self.batches[round_no].handoff_unresolved())
            self._log("handoff_unresolved", {
                "order_ids": [claim.order_id for claim in moved],
            })
            return moved

    def finance_recalc(self, order_id: str, *, include: bool = True) -> ConversionClaim:
        with self.lock:
            found = self._find_claim(order_id)
            if found is None:
                raise KeyError(f"订单 {order_id} 不属于实验 {self.experiment_id}")
            batch, _ = found
            claim = batch.settle_finance(order_id, include=include)
            self._log("finance_recalc", {"order_id": order_id, "include": include})
            return claim

    def transfer_claim(self, order_id: str, owner: str) -> dict[str, Any]:
        with self.lock:
            found = self._find_claim(order_id)
            if found is None:
                raise KeyError(f"订单 {order_id} 不属于实验 {self.experiment_id}")
            batch, _ = found
            batch.transfer_claim(order_id, owner)
            return self.claim_state(order_id)

    def claim_state(self, order_id: str) -> dict[str, Any]:
        """核对原订单当前归属：已计入结案还是留作本次更正。"""
        with self.lock:
            found = self._find_claim(order_id)
            if found is None:
                raise KeyError(f"订单 {order_id} 不属于实验 {self.experiment_id}")
            batch, claim = found
            state = claim.to_dict()
            state.update({
                "batch_id": batch.batch_id,
                "batch_status": batch.status.value,
                "basis_complete": batch.basis_complete,
                "closure_revision": (
                    batch.close_snapshot.get("revision") if batch.close_snapshot else None
                ),
                "in_open_diff": claim.disposition == ConversionDisposition.DEFERRED,
            })
            return state

    # ---------- 报表 ----------

    def batches_payload(self) -> list[dict[str, Any]]:
        with self.lock:
            return [self.batches[round_no].to_dict() for round_no in sorted(self.batches)]

    def daily_report(self, report_date: str | None = None) -> dict[str, Any]:
        """日报：只使用依据完整的实验结果。

        存在未处置差异（``basis_complete`` 为假）的批次不会进入 ``experiments``，
        而是进入 ``skipped`` 并给出原因；财务已复算的订单单列在 ``corrections``，
        不会与实验结案口径重复计数。
        """
        with self.lock:
            experiments: list[dict[str, Any]] = []
            skipped: list[dict[str, Any]] = []
            corrections: list[dict[str, Any]] = []
            pending_rounds: list[int] = []
            incomplete_rounds: list[int] = []
            for round_no in sorted(self.batches):
                batch = self.batches[round_no]
                if batch.close_snapshot is None:
                    pending_rounds.append(round_no)
                    skipped.append({
                        "batch_id": batch.batch_id,
                        "experiment_id": self.experiment_id,
                        "round_no": round_no,
                        "kind": "pending_close",
                        "reason": "尚未结案",
                    })
                    continue
                if not batch.basis_complete:
                    incomplete_rounds.append(round_no)
                    skipped.append({
                        "batch_id": batch.batch_id,
                        "experiment_id": self.experiment_id,
                        "round_no": round_no,
                        "kind": "incomplete_basis",
                        "reason": "存在未处置差异，依据不完整",
                        "unresolved_order_ids": sorted(batch.unresolved),
                    })
                    continue
                # 日报采用结案快照的数字，结案后发生的更正不会回写当次结案口径。
                live = dict(batch.close_snapshot)
                live.update({
                    "batch_status": batch.status.value,
                    "basis_complete": batch.basis_complete,
                    "close_snapshot_denominator": batch.close_snapshot.get("denominator"),
                    "close_snapshot_numerator": batch.close_snapshot.get("numerator"),
                    "close_snapshot_revision": batch.close_snapshot.get("revision"),
                    "current_numerator": batch.recompute()["numerator"],
                    "correction_order_ids": sorted(
                        claim.order_id for claim in batch.claims.values()
                        if claim.counted_in_correction
                    ),
                    "pending_correction_order_ids": sorted(
                        claim.order_id for claim in batch.claims.values()
                        if claim.disposition == ConversionDisposition.DEFERRED
                    ),
                })
                experiments.append(live)
                for claim in batch.claims.values():
                    if claim.counted_in_correction:
                        corrections.append({
                            "original_order_id": claim.order_id,
                            "experiment_id": claim.experiment_id,
                            "batch_id": batch.batch_id,
                            "round_no": claim.round_no,
                            "user_id": claim.user_id,
                            "ordered_at": _iso(claim.ordered_at),
                            "disposition": claim.disposition.value,
                            "owner": claim.owner,
                            "scope": "correction",
                        })
            return {
                "report_date": report_date,
                "experiment_id": self.experiment_id,
                "experiments": experiments,
                "corrections": corrections,
                "skipped": skipped,
                # 只有「依据不完整」会阻断日报；后续轮次尚未结案只是还没到期，
                # 不应让已结案轮次的日报看起来不可用。
                "complete": not incomplete_rounds,
                "incomplete_rounds": incomplete_rounds,
                "pending_rounds": pending_rounds,
            }

    def counts(self) -> dict[str, int]:
        with self.lock:
            experiment_orders: set[str] = set()
            finance_orders: set[str] = set()
            for batch in self.batches.values():
                for claim in batch.claims.values():
                    if claim.counted_in_closure:
                        experiment_orders.add(claim.order_id)
                    if claim.counted_in_correction:
                        finance_orders.add(claim.order_id)
            return {
                "experiment_orders": len(experiment_orders),
                "finance_orders": len(finance_orders),
                "total_claimed": len(experiment_orders | finance_orders),
                "overlap": len(experiment_orders & finance_orders),
            }

    def _log(self, event: str, payload: dict[str, Any]) -> None:
        self.history.append({"event": event, **payload})

    def to_dict(self) -> dict[str, Any]:
        with self.lock:
            return {
                "experiment_id": self.experiment_id,
                "rounds": [rnd.to_dict() for rnd in self.rounds],
                "batches": self.batches_payload(),
                "counts": self.counts(),
                "history": list(self.history),
            }


# ============================================================
# 指标对比 / 日报的适配
# ============================================================

def build_metric_comparison(batch: ValidConversionBatch) -> dict[str, Any]:
    """把有效转化批次转换成实验指标对比口径。

    入组人数来自批次分母，购买数来自真正发生且通过时点校验的订单。
    """
    metrics = batch.recompute()
    return {
        "batch_id": batch.batch_id,
        "experiment_id": batch.experiment_id,
        "round_no": batch.round_no,
        "入组人数": metrics["denominator"],
        "有效购买数": metrics["numerator"],
        "转化率": metrics["conversion_rate"],
        "依据状态": "依据完整" if batch.basis_complete else "依据不完整",
        "对比状态": "已完成" if batch.basis_complete else "数据缺失",
        "分母批次": batch.batch_id,
        "分母用户": metrics["enrolled_user_ids"],
        "分子订单": metrics["purchase_order_ids"],
        "未处置订单": sorted(batch.unresolved),
    }


def build_daily_report(
    ledgers: Iterable[ExperimentClosureLedger],
    report_date: str | None = None,
) -> dict[str, Any]:
    """汇总多个实验账本的日报，仅保留依据完整的结果。"""
    experiments: list[dict[str, Any]] = []
    corrections: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    incomplete = False
    for ledger in ledgers:
        report = ledger.daily_report(report_date)
        experiments.extend(report["experiments"])
        corrections.extend(report["corrections"])
        skipped.extend(report["skipped"])
        incomplete = incomplete or not report["complete"]
    return {
        "report_date": report_date,
        "experiments": experiments,
        "corrections": corrections,
        "skipped": skipped,
        "complete": not incomplete,
    }
