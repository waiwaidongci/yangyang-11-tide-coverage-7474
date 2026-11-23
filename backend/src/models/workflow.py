from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from src.constants.enums import RunStatus, VersionStatus
from src.models.base import Base, utc_now


class Observation(Base):
    __tablename__ = 'observations'
    id: Mapped[int] = mapped_column(primary_key=True)
    source_key: Mapped[str] = mapped_column(String(96), unique=True)
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)


class AnalysisRun(Base):
    __tablename__ = 'analysis_runs'
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(120))
    owner: Mapped[str] = mapped_column(String(60))
    parameters: Mapped[dict] = mapped_column(JSON)
    last_observation_id: Mapped[int] = mapped_column(Integer)
    sample_size: Mapped[int] = mapped_column(Integer)
    observation_ids: Mapped[list] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(16), default=RunStatus.QUEUED, index=True)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Review(Base):
    __tablename__ = 'reviews'
    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey('analysis_runs.id'), unique=True)
    decision: Mapped[str] = mapped_column(String(16))
    note: Mapped[str] = mapped_column(Text)
    reviewer: Mapped[str] = mapped_column(String(60))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)


class Schedule(Base):
    __tablename__ = 'schedules'
    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey('analysis_runs.id'), index=True)
    recipient: Mapped[str] = mapped_column(String(100))
    interval_minutes: Mapped[int] = mapped_column(Integer)
    next_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Delivery(Base):
    __tablename__ = 'deliveries'
    __table_args__ = (UniqueConstraint('schedule_id', 'scheduled_at'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    schedule_id: Mapped[int] = mapped_column(ForeignKey('schedules.id'))
    scheduled_at: Mapped[datetime] = mapped_column(DateTime)
    filename: Mapped[str] = mapped_column(String(120))
    delivered_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)


class AuditEvent(Base):
    __tablename__ = 'audit_events'
    id: Mapped[int] = mapped_column(primary_key=True)
    actor: Mapped[str] = mapped_column(String(60))
    action: Mapped[str] = mapped_column(String(60))
    detail: Mapped[str] = mapped_column(String(200))
    at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)


class WindowCorrection(Base):
    """One tide-window correction covering one or more zones for a single shift.

    A correction carries the full set of workable/closed segments it declares.
    Corrections are keyed by an idempotency code so a resubmitted request does
    not create a duplicate.
    """

    __tablename__ = 'window_corrections'
    __table_args__ = (UniqueConstraint('idempotency_key'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    work_date: Mapped[str] = mapped_column(String(10), index=True)
    shift: Mapped[str] = mapped_column(String(8), index=True)
    code: Mapped[str] = mapped_column(String(96))
    idempotency_key: Mapped[str] = mapped_column(String(96))
    submitted_by: Mapped[str] = mapped_column(String(60))
    target_coverage_pct: Mapped[float] = mapped_column(default=90.0)
    # Effective instant of the correction; latest effective correction wins
    # when two corrections touch the same minute.
    issued_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, index=True)
    segments: Mapped[list] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)


class ShiftReport(Base):
    """The current (and historical) version of a shift report.

    Exactly one row per (work_date, shift) may be ``current``. Recalculations
    create a new version from the original shift's completion tracks; the
    previous current version is superseded, never deleted, so old versions and
    their source corrections remain queryable.

    ``signature`` identifies the exact correction set the version was computed
    from, which makes a retry of a failed recalculation reuse the same row
    instead of producing a duplicate report. ``watermark`` is the highest
    correction id included, used to keep an older recalculation that finishes
    late from overwriting a newer current version.
    """

    __tablename__ = 'shift_reports'
    __table_args__ = (UniqueConstraint('work_date', 'shift', 'signature'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    work_date: Mapped[str] = mapped_column(String(10), index=True)
    shift: Mapped[str] = mapped_column(String(8), index=True)
    version: Mapped[int] = mapped_column(Integer)
    signature: Mapped[str] = mapped_column(String(64))
    watermark: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), default=VersionStatus.QUEUED, index=True)
    is_current: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    parameters: Mapped[dict] = mapped_column(JSON)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Snapshot of the original shift's observations (bulletin + tracks).
    observation_ids: Mapped[list] = mapped_column(JSON)
    correction_ids: Mapped[list] = mapped_column(JSON)
    triggered_correction_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    owner: Mapped[str] = mapped_column(String(60))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
