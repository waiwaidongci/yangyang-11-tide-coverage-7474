"""Tide-window corrections and versioned shift reports.

A correction declares workable/closed periods for one or more zones of a
single work date + shift. Corrections are append-only; when two corrections
touch the same minute, the one signed latest wins, and minutes no correction
touches fall back to the original bulletin. Submitting a correction queues a
recalculation of that shift.

The recalculation always uses the *original* shift report's completion tracks
(and the original bulletin as the base layer); corrections only change which
minutes are workable. Corrections for a shift are serialized so that a
submission always sees the corrections committed before it, and each queued
version snapshots the full correction set at that point — so a correction whose
periods were not later overridden is still counted, and the newest signed
correction wins each minute.

A version only becomes the shift page's current report once it finishes, and
the current version is always the successfully computed version covering the
newest correction set. Thus a slower recalculation that started earlier can
never make the page fall back to an older version. Superseded versions and the
corrections they used are retained for lookup.
"""

import hashlib
import json
import logging

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from src.constants.enums import Role, VersionStatus
from src.domain.analysis import DEFAULT_TARGET, analyze_shift, normalize_correction
from src.models.base import utc_now
from src.models.workflow import AuditEvent, Observation, ShiftReport, WindowCorrection
from src.services.errors import BusinessError

logger = logging.getLogger(__name__)


def _digest(values: list[int]) -> str:
    return hashlib.sha256(",".join(str(item) for item in sorted(values)).encode()).hexdigest()


def _corrections_for(db, work_date: str, shift: str) -> list[WindowCorrection]:
    return list(db.scalars(
        select(WindowCorrection)
        .where(WindowCorrection.work_date == work_date, WindowCorrection.shift == shift)
        .order_by(WindowCorrection.issued_at, WindowCorrection.id)
    ))


def shift_observation_ids(db, work_date: str, shift: str) -> list[int]:
    rows = db.execute(select(Observation.id, Observation.payload).order_by(Observation.id)).all()
    return [row.id for row in rows
            if row.payload.get("work_date") == work_date and row.payload.get("shift") == shift]


def _lock_shift(db, work_date: str, shift: str) -> None:
    """Serialise corrections and report versions within one shift.

    Every writer for a shift locks the shift's oldest report row (its baseline
    version). That row is created before any correction and never deleted, so
    it is a stable per-shift mutex: a submission that has the lock sees every
    correction committed before it, which means a version snapshot always
    contains all corrections whose periods are still in effect. The worker
    takes the same row first, so submission and computation share one lock
    order and cannot deadlock.
    """

    ensure_baseline(db, work_date, shift)
    report_id = db.scalar(
        select(func.min(ShiftReport.id))
        .where(ShiftReport.work_date == work_date, ShiftReport.shift == shift)
    )
    if report_id is None:
        return
    db.execute(select(ShiftReport).where(ShiftReport.id == report_id).with_for_update()).scalar_one()


def ensure_baseline(db, work_date: str, shift: str) -> ShiftReport | None:
    """Create the original (correction-free) version if the shift has none.

    Returns ``None`` when the shift has neither completion tracks nor a tide
    bulletin, since there is nothing to report yet.
    """

    existing = db.scalar(
        select(ShiftReport)
        .where(ShiftReport.work_date == work_date, ShiftReport.shift == shift)
        .order_by(ShiftReport.id).limit(1)
    )
    if existing is not None:
        return existing
    observation_ids = shift_observation_ids(db, work_date, shift)
    if not observation_ids:
        return None
    report = ShiftReport(
        work_date=work_date,
        shift=shift,
        version=1,
        signature=_digest([]),
        watermark=0,
        status=VersionStatus.QUEUED,
        is_current=False,
        parameters={"work_date": work_date, "shift": shift, "target_coverage_pct": DEFAULT_TARGET},
        observation_ids=observation_ids,
        correction_ids=[],
        triggered_correction_id=None,
        owner="system",
    )
    try:
        with db.begin_nested():
            db.add(report)
    except IntegrityError:
        return db.scalar(select(ShiftReport).where(
            ShiftReport.work_date == work_date,
            ShiftReport.shift == shift,
            ShiftReport.signature == _digest([]),
        ))
    return report


def _enqueue_version(db, work_date: str, shift: str, corrections: list[WindowCorrection],
                     actor: str, triggered: WindowCorrection | None,
                     target: float = DEFAULT_TARGET) -> ShiftReport:
    correction_ids = [item.id for item in corrections]
    signature = _digest(correction_ids)
    existing = db.scalar(select(ShiftReport).where(
        ShiftReport.work_date == work_date,
        ShiftReport.shift == shift,
        ShiftReport.signature == signature,
    ))
    if existing is not None:
        return existing

    baseline = ensure_baseline(db, work_date, shift)
    if baseline is None:
        raise BusinessError(f'{work_date} {shift} 还没有完工轨迹或潮窗台账，请先录入后再更正。', 409)

    highest = db.scalar(select(func.max(ShiftReport.version)).where(
        ShiftReport.work_date == work_date, ShiftReport.shift == shift)) or 0
    report = ShiftReport(
        work_date=work_date,
        shift=shift,
        version=highest + 1,
        signature=signature,
        watermark=max(correction_ids) if correction_ids else 0,
        status=VersionStatus.QUEUED,
        is_current=False,
        parameters={"work_date": work_date, "shift": shift, "target_coverage_pct": target},
        observation_ids=list(baseline.observation_ids),
        correction_ids=correction_ids,
        triggered_correction_id=triggered.id if triggered else None,
        owner=actor,
    )
    try:
        with db.begin_nested():
            db.add(report)
    except IntegrityError:
        return db.scalar(select(ShiftReport).where(
            ShiftReport.work_date == work_date,
            ShiftReport.shift == shift,
            ShiftReport.signature == signature,
        ))
    db.add(AuditEvent(actor=actor, action='report.queued',
                      detail=f'{work_date} {shift} 第 {report.version} 版，含 {len(correction_ids)} 份更正'))
    return report


def submit_correction(db, payload, actor) -> tuple[WindowCorrection, ShiftReport]:
    try:
        clean = normalize_correction(payload)
    except ValueError as exc:
        raise BusinessError(str(exc)) from exc

    key = (payload.get("idempotency_key") or "").strip()
    if len(key) > 96:
        raise BusinessError("幂等键过长。")
    if not key:
        canonical = json.dumps(
            {"date": clean["work_date"], "shift": clean["shift"], "code": clean["code"],
             "segments": clean["segments"]},
            sort_keys=True, ensure_ascii=False,
        )
        key = "data:" + hashlib.sha256(canonical.encode()).hexdigest()

    _lock_shift(db, clean["work_date"], clean["shift"])

    existing = db.scalar(select(WindowCorrection).where(WindowCorrection.idempotency_key == key))
    if existing is not None:
        corrections = _corrections_for(db, existing.work_date, existing.shift)
        report = _enqueue_version(db, existing.work_date, existing.shift, corrections, actor, existing,
                                  existing.target_coverage_pct)
        return existing, report

    correction = WindowCorrection(
        work_date=clean["work_date"],
        shift=clean["shift"],
        code=clean["code"],
        idempotency_key=key,
        submitted_by=actor,
        issued_at=utc_now(),
        segments=clean["segments"],
        target_coverage_pct=clean["target_coverage_pct"],
    )
    try:
        with db.begin_nested():
            db.add(correction)
    except IntegrityError as exc:
        raise BusinessError("有相同更正刚刚提交，请刷新后查看。", 409) from exc
    db.add(AuditEvent(actor=actor, action="correction.created",
                      detail=f"{correction.work_date} {correction.shift} {correction.code} · "
                             f"{len(correction.segments)} 段"))

    corrections = _corrections_for(db, correction.work_date, correction.shift)
    report = _enqueue_version(db, correction.work_date, correction.shift, corrections, actor, correction,
                              correction.target_coverage_pct)
    return correction, report


def retry_report(db, report_id: int, actor) -> ShiftReport:
    row = db.get(ShiftReport, report_id, with_for_update=True)
    if row is None:
        raise BusinessError("班报版本不存在", 404)
    if row.status != VersionStatus.FAILED:
        raise BusinessError("只有补算失败的版本可以重试。", 409)
    row.status = VersionStatus.QUEUED
    row.error = None
    row.result = None
    row.finished_at = None
    db.add(AuditEvent(actor=actor, action="report.retried",
                      detail=f"{row.work_date} {row.shift} 第 {row.version} 版重试补算"))
    db.flush()
    return row


def _correction_payload(row: WindowCorrection) -> dict:
    return {
        "id": row.id,
        "code": row.code,
        "issued_at": row.issued_at.isoformat() if row.issued_at else None,
        "segments": row.segments,
    }


def _promote_current(db, row: ShiftReport) -> None:
    """Set the shift's current version to the newest successfully computed one."""

    rows = list(db.scalars(select(ShiftReport).where(
        ShiftReport.work_date == row.work_date,
        ShiftReport.shift == row.shift,
    )))
    computed = [item for item in rows if item.status == VersionStatus.SUCCEEDED]
    winner = max(computed, key=lambda item: (item.watermark, item.id)) if computed else None
    for item in rows:
        item.is_current = winner is not None and item.id == winner.id


def _compute(db, row: ShiftReport) -> None:
    try:
        observations = list(db.scalars(
            select(Observation.payload).where(Observation.id.in_(row.observation_ids)).order_by(Observation.id)
        ))
        corrections = list(db.scalars(
            select(WindowCorrection)
            .where(WindowCorrection.id.in_(row.correction_ids))
            .order_by(WindowCorrection.issued_at, WindowCorrection.id)
        )) if row.correction_ids else []
        result = analyze_shift(observations, [_correction_payload(item) for item in corrections], row.parameters)
        if not {"headline", "summary", "metrics", "columns", "rows"} <= set(result):
            raise RuntimeError("invalid result schema")
        json.dumps(result, allow_nan=False, ensure_ascii=False)
        row.result = result
        row.status = VersionStatus.SUCCEEDED
    except ValueError as exc:
        row.error = str(exc)[:600] or "本次观测与更正不足以完成计算"
        row.status = VersionStatus.FAILED
    except Exception:
        logger.exception("Shift report %s raised unexpectedly", row.id)
        row.error = "班报补算失败，请检查记录和更正后重试"
        row.status = VersionStatus.FAILED
    row.finished_at = utc_now()
    _promote_current(db, row)


def run_pending_reports(db) -> bool:
    """Compute one queued shift report, one shift at a time."""

    candidate_id = db.scalar(
        select(ShiftReport.id).where(ShiftReport.status == VersionStatus.QUEUED)
        .order_by(ShiftReport.id).limit(1)
    )
    if candidate_id is None:
        return False
    candidate = db.get(ShiftReport, candidate_id)
    if candidate is None:
        return False
    # Serialise the whole shift so two versions finishing together cannot both
    # promote themselves. Lock the oldest report row first (stable order).
    anchor_id = db.scalar(select(func.min(ShiftReport.id)).where(
        ShiftReport.work_date == candidate.work_date,
        ShiftReport.shift == candidate.shift,
    ))
    db.execute(select(ShiftReport).where(ShiftReport.id == anchor_id).with_for_update()).scalar_one()
    row = db.execute(
        select(ShiftReport).where(ShiftReport.id == candidate_id).with_for_update()
    ).scalar_one_or_none()
    if row is None or row.status != VersionStatus.QUEUED:
        return False
    _compute(db, row)
    return True


def visible_reports(db, user, report_id: int | None = None):
    query = select(ShiftReport)
    if user["role"] == Role.VIEWER:
        query = query.where(ShiftReport.status == VersionStatus.SUCCEEDED)
    if report_id is not None:
        row = db.scalar(query.where(ShiftReport.id == report_id))
        if row is None:
            raise BusinessError("没有可查看的班报版本", 404)
        return row
    return list(db.scalars(query.order_by(ShiftReport.work_date.desc(), ShiftReport.shift, ShiftReport.id.desc())))


def current_report(db, work_date: str, shift: str):
    return db.scalar(select(ShiftReport).where(
        ShiftReport.work_date == work_date,
        ShiftReport.shift == shift,
        ShiftReport.is_current.is_(True),
    ))


def reports_for_shift(db, work_date: str, shift: str) -> list[ShiftReport]:
    return list(db.scalars(select(ShiftReport).where(
        ShiftReport.work_date == work_date,
        ShiftReport.shift == shift,
    ).order_by(ShiftReport.version.desc(), ShiftReport.id.desc())))


def corrections_for_shift(db, work_date: str, shift: str) -> list[WindowCorrection]:
    return _corrections_for(db, work_date, shift)


def corrections_by_ids(db, ids: list[int]) -> list[WindowCorrection]:
    if not ids:
        return []
    rows = list(db.scalars(select(WindowCorrection).where(WindowCorrection.id.in_(ids))))
    order = {value: index for index, value in enumerate(ids)}
    return sorted(rows, key=lambda row: order.get(row.id, 0))
