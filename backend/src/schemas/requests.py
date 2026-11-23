from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from src.constants.enums import Decision


class Credentials(BaseModel):
    email: str = Field(min_length=1, max_length=60)
    password: str = Field(min_length=1, max_length=128)


class CreateRun(BaseModel):
    title: str = Field(max_length=120)
    parameters: dict = Field(default_factory=dict)

    @field_validator('title')
    @classmethod
    def meaningful_title(cls, value):
        if not value.strip():
            raise ValueError('请填写分析名称')
        return value.strip()


class ReviewInput(BaseModel):
    decision: Decision
    note: str = Field(min_length=2, max_length=1000)

    @field_validator('note')
    @classmethod
    def meaningful_note(cls, value):
        if len(value.strip()) < 2:
            raise ValueError('请填写复核依据')
        return value.strip()


class ScheduleInput(BaseModel):
    run_id: int = Field(gt=0)
    recipient: str = Field(min_length=2, max_length=100)
    interval_minutes: int = Field(ge=1, le=10080)
    start_at: datetime | None = None

    @field_validator('recipient')
    @classmethod
    def meaningful_recipient(cls, value):
        if len(value.strip()) < 2:
            raise ValueError('请填写收件组')
        return value.strip()

    @field_validator('start_at')
    @classmethod
    def absolute_time(cls, value):
        if value is not None and value.utcoffset() is None:
            raise ValueError('首次分发时间必须包含时区')
        return value


class CorrectionSegment(BaseModel):
    zone: str = Field(min_length=1, max_length=20)
    action: str = Field(default='可作业', max_length=8)
    start_minute: int = Field(ge=0, le=1440)
    end_minute: int = Field(ge=1, le=1440)
    frontage_m: int | None = None


class CorrectionInput(BaseModel):
    work_date: str = Field(min_length=10, max_length=10)
    shift: str = Field(min_length=1, max_length=8)
    code: str = Field(min_length=1, max_length=96)
    segments: list[CorrectionSegment] = Field(min_length=1, max_length=200)
    target_coverage_pct: float = Field(default=90, ge=0, le=100)
    idempotency_key: str | None = Field(default=None, max_length=96)

    @field_validator('code')
    @classmethod
    def meaningful_code(cls, value):
        if not value.strip():
            raise ValueError('请填写潮窗更正编号')
        return value.strip()
