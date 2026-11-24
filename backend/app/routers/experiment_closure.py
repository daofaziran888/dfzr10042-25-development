"""实验结案与有效转化批次接口。

挂在资源路由之前注册，避免 ``/api/{module}`` 的动态段抢先匹配。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.services.valid_conversion_batch import ValidConversionBatchService

router = APIRouter(prefix="/api/experiment-closure", tags=["实验结案"])
service = ValidConversionBatchService()


class RoundPayload(BaseModel):
    experiment_id: str
    round_no: int = 1
    start_at: str
    cutoff_at: str
    round_duration_minutes: int = 60
    auto_next_round: bool = True


class EnrollPayload(BaseModel):
    experiment_id: str
    user_id: str
    enrolled_at: str


class ConversionPayload(BaseModel):
    experiment_id: str
    order_id: str
    user_id: str
    ordered_at: str
    reported_at: str | None = None
    source: str = "late"
    round_no: int | None = None


class ClosePayload(BaseModel):
    experiment_id: str
    round_no: int | None = None
    closed_at: str | None = None


class FinancePayload(BaseModel):
    experiment_id: str
    order_id: str
    include: bool = True


class TransferPayload(BaseModel):
    experiment_id: str
    order_id: str
    owner: str = Field(description="experiment 或 finance")


def _guard(func, *args, **kwargs):
    try:
        return func(*args, **kwargs)
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.post("/rounds")
def configure_round(payload: RoundPayload) -> dict[str, Any]:
    return _guard(
        service.configure_round,
        payload.experiment_id,
        payload.round_no,
        payload.start_at,
        payload.cutoff_at,
        round_duration_minutes=payload.round_duration_minutes,
        auto_next_round=payload.auto_next_round,
    )


@router.post("/enrollments")
def enroll(payload: EnrollPayload) -> dict[str, Any]:
    return _guard(service.enroll, payload.experiment_id, payload.user_id, payload.enrolled_at)


@router.post("/conversions")
def report_conversion(payload: ConversionPayload) -> dict[str, Any]:
    return _guard(
        service.report_conversion,
        payload.experiment_id,
        order_id=payload.order_id,
        user_id=payload.user_id,
        ordered_at=payload.ordered_at,
        reported_at=payload.reported_at,
        source=payload.source,
        round_no=payload.round_no,
    )


@router.post("/close")
def close_experiment(payload: ClosePayload) -> dict[str, Any]:
    if payload.round_no is None:
        return _guard(service.close, payload.experiment_id, payload.closed_at)
    return _guard(service.close_round, payload.experiment_id, payload.round_no, payload.closed_at)


@router.get("/{experiment_id}/reaudit")
def reaudit(experiment_id: str) -> dict[str, Any]:
    return _guard(service.reaudit, experiment_id)


@router.get("/daily-report")
def daily_report(experiment_id: str | None = None, report_date: str | None = None) -> dict[str, Any]:
    return _guard(service.daily_report, experiment_id, report_date)


@router.get("/daily-report/{experiment_id}")
def daily_report_scoped(experiment_id: str, report_date: str | None = None) -> dict[str, Any]:
    return _guard(service.daily_report, experiment_id, report_date)


@router.get("/{experiment_id}/daily-report")
def daily_report_for(experiment_id: str, report_date: str | None = None) -> dict[str, Any]:
    return _guard(service.daily_report, experiment_id, report_date)


@router.post("/handoff")
def handoff(payload: ClosePayload) -> dict[str, Any]:
    return _guard(service.handoff, payload.experiment_id)


@router.post("/finance-recalc")
def finance_recalc(payload: FinancePayload) -> dict[str, Any]:
    return _guard(
        service.finance_recalc, payload.experiment_id, payload.order_id,
        include=payload.include,
    )


@router.post("/transfer")
def transfer(payload: TransferPayload) -> dict[str, Any]:
    return _guard(service.transfer, payload.experiment_id, payload.order_id, payload.owner)


@router.get("/{experiment_id}/metric-comparison")
def metric_comparison(experiment_id: str, round_no: int | None = None) -> dict[str, Any]:
    return _guard(service.metric_comparison, experiment_id, round_no)


@router.get("/{experiment_id}/comparison")
def comparison(experiment_id: str, round_no: int | None = None) -> dict[str, Any]:
    """指标对比数据面：纯计算结果 + 已写入对比方案行。"""
    return _guard(service.comparison_payload, experiment_id, round_no)


@router.get("/{experiment_id}/claims/{order_id}")
def claim_state(experiment_id: str, order_id: str) -> dict[str, Any]:
    return _guard(service.claim_state, experiment_id, order_id)


@router.get("/{experiment_id}")
def experiment_snapshot(experiment_id: str) -> dict[str, Any]:
    return _guard(service.snapshot, experiment_id)
