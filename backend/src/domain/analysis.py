"""Reconcile cleaning tracks against tide windows and surveyed zone frontage."""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date

WINDOW = "可作业潮窗"
TRACK = "清扫完工轨迹"
SHIFTS = ("早班", "下午班")
FRONTAGE_M = {"北栈桥": 240, "贝壳湾": 160, "南礁滩": 600}
ZONES = tuple(FRONTAGE_M)
DEFAULT_DATE = "2026-11-17"
DEFAULT_SHIFT = SHIFTS[0]
DEFAULT_TARGET = 90

OVERLAP = "窗内重叠"
OUTSIDE = "窗外完工"
GAP = "待补扫"

# Each zone lists approved windows followed by completed tracks, in minutes
# since midnight. Some tide bulletins intentionally overlap at their handoff.
SHIFT_LOGS = (
    (DEFAULT_DATE, DEFAULT_SHIFT, (
        (((360, 450), (440, 480)), ((350, 400), (390, 435), (445, 480), (480, 490))),
        (((375, 435), (465, 525)), ((380, 420), (410, 475), (470, 510), (500, 525))),
        (((390, 510),), ((400, 445), (446, 475), (490, 505))),
    )),
    (DEFAULT_DATE, SHIFTS[1], (
        (((780, 870),), ((780, 824), (824, 870))),
        (((795, 885),), ((795, 840), (840, 881))),
        (((810, 900),), ((812, 855), (855, 899))),
    )),
    ("2026-11-18", DEFAULT_SHIFT, (
        (((385, 475),), ((385, 422), (430, 475))),
        (((400, 490),), ((400, 450), (460, 490))),
        (((420, 510),), ((424, 460), (468, 506))),
    )),
)


def seed() -> list[dict]:
    # Omit redundant frontage so these source records retain their content keys.
    rows = []
    for shift_no, (work_date, shift, zones) in enumerate(SHIFT_LOGS, 1):
        for zone_no, (zone, (windows, tracks)) in enumerate(zip(ZONES, zones, strict=True), 1):
            for kind, prefix, intervals in ((WINDOW, "CW", windows), (TRACK, "TR", tracks)):
                for record_no, (start, end) in enumerate(intervals, 1):
                    rows.append({
                        "work_date": work_date,
                        "shift": shift,
                        "zone": zone,
                        "record_kind": kind,
                        "record_code": f"{prefix}-{shift_no}{zone_no}-{record_no:02d}",
                        "start_minute": start,
                        "end_minute": end,
                    })
    return rows


def _validate_date(value: str) -> None:
    try:
        if date.fromisoformat(value).isoformat() == value:
            return
    except (TypeError, ValueError):
        pass
    raise ValueError("作业日期须为有效的 YYYY-MM-DD 日期。")


def validate(row: dict) -> None:
    _validate_date(row.get("work_date"))
    if row.get("shift") not in SHIFTS:
        raise ValueError("请选择早班或下午班。")
    zone = row.get("zone")
    if zone not in ZONES:
        raise ValueError("请选择有效的海滩分区。")
    if row.get("record_kind") not in (WINDOW, TRACK):
        raise ValueError("记录类型须为可作业潮窗或清扫完工轨迹。")
    code = row.get("record_code")
    if not isinstance(code, str) or not code.strip():
        raise ValueError("来源记录编号不能为空。")
    if "frontage_m" in row:
        frontage = row["frontage_m"]
        if type(frontage) not in (int, float) or frontage != FRONTAGE_M[zone]:
            raise ValueError(f"记录 {code.strip()} 的{zone}岸线长度须与分区台账一致（{FRONTAGE_M[zone]} 米）。")
    start, end = row.get("start_minute"), row.get("end_minute")
    for point in (start, end):
        if type(point) not in (int, float) or not 0 <= point <= 1440 or int(point) != point:
            raise ValueError("开始和结束时间须填写 0 至 1440 之间的整分钟。")
    if start >= end:
        raise ValueError("结束分钟必须晚于开始分钟。跨日轨迹须先按日期拆分。")


@dataclass
class Issue:
    kind: str
    start: int
    end: int
    codes: tuple[str, ...]


@dataclass
class ZoneCoverage:
    eligible: int = 0
    raw: int = 0
    clipped: int = 0
    covered: int = 0
    outside: int = 0
    issues: list[Issue] = field(default_factory=list)

    @property
    def overlap(self) -> int:
        return self.clipped - self.covered

    def add_issue(self, kind: str, start: int, end: int, codes: set[str]) -> None:
        sources = tuple(sorted(codes))
        if self.issues:
            last = self.issues[-1]
            if last.kind == kind and last.end == start and last.codes == sources:
                last.end = end
                return
        self.issues.append(Issue(kind, start, end, sources))


def _sweep(records: list[dict]) -> ZoneCoverage:
    events = defaultdict(list)
    for row in records:
        kind, code = row["record_kind"], row["record_code"].strip()
        events[int(row["start_minute"])].append((kind, code, True))
        events[int(row["end_minute"])].append((kind, code, False))

    coverage = ZoneCoverage()
    active = {WINDOW: set(), TRACK: set()}
    previous = 0
    for minute in sorted(events):
        span = minute - previous
        windows, tracks = active[WINDOW], active[TRACK]
        count = len(tracks)
        coverage.raw += count * span
        if windows:
            coverage.eligible += span
            coverage.clipped += count * span
            if tracks:
                coverage.covered += span
                if count > 1:
                    coverage.add_issue(OVERLAP, previous, minute, tracks)
            else:
                coverage.add_issue(GAP, previous, minute, windows)
        elif tracks:
            coverage.outside += span
            coverage.add_issue(OUTSIDE, previous, minute, tracks)

        for kind, code, opens in events[minute]:
            if opens:
                active[kind].add(code)
            else:
                active[kind].remove(code)
        previous = minute
    return coverage


def _clock(minute: int) -> str:
    return f"{minute // 60:02d}:{minute % 60:02d}"


def _issue_row(zone: str, issue: Issue) -> dict:
    duration = issue.end - issue.start
    return {
        "zone": zone,
        "kind": f"{OUTSIDE}（含重叠）" if issue.kind == OUTSIDE and len(issue.codes) > 1 else issue.kind,
        "period": f"{_clock(issue.start)}–{_clock(issue.end)}",
        "sources": "、".join(issue.codes),
        "frontage_m": FRONTAGE_M[zone],
        "eligible_min": duration if issue.kind == GAP else None,
        "eligible_m_min": duration * FRONTAGE_M[zone] if issue.kind == GAP else None,
        "raw_min": duration * len(issue.codes) if issue.kind != GAP else None,
        "covered_min": duration if issue.kind == OVERLAP else None,
        "covered_m_min": duration * FRONTAGE_M[zone] if issue.kind == OVERLAP else None,
        "overlap_min": duration * (len(issue.codes) - 1) if issue.kind == OVERLAP else None,
        "outside_min": duration if issue.kind == OUTSIDE else None,
        "coverage_pct": None,
    }


def _weight_explanation(
    by_zone: dict[str, ZoneCoverage], eligible: int, covered: int,
    eligible_m_min: int, covered_m_min: int,
) -> str:
    difference = covered_m_min * eligible - covered * eligible_m_min
    if difference == 0:
        return "本班按两种口径汇总的覆盖率相同。"

    direction = 1 if difference > 0 else -1
    # Select the zone contributing most in the direction of the difference.
    # Each contribution is the change in its denominator share times its
    # coverage deviation from the unweighted shift average, with common factors removed.
    zone, item = max(
        ((zone, item) for zone, item in by_zone.items() if item.eligible),
        key=lambda pair: direction * (FRONTAGE_M[pair[0]] * eligible - eligible_m_min)
        * (pair[1].covered * eligible - covered * pair[1].eligible),
    )
    frontage = FRONTAGE_M[zone]
    time_share = 100 * item.eligible / eligible
    weighted_share = 100 * frontage * item.eligible / eligible_m_min
    zone_rate = 100 * item.covered / item.eligible
    change = "提高" if difference > 0 else "拉低"
    return (f"{zone}岸线长 {frontage} 米，在分母中按分钟占 {time_share:.2f}%，"
            f"按岸线米·分钟占 {weighted_share:.2f}%，分区覆盖率为 {zone_rate:.2f}%，"
            f"是加权结果{change}的主要原因。")


def analyze(rows: list[dict], parameters: dict) -> dict:
    work_date = parameters.get("work_date", DEFAULT_DATE)
    shift = parameters.get("shift", DEFAULT_SHIFT)
    target = parameters.get("target_coverage_pct", DEFAULT_TARGET)
    _validate_date(work_date)
    if shift not in SHIFTS:
        raise ValueError("请选择要复核的早班或下午班。")
    if type(target) not in (int, float) or not 0 <= target <= 100:
        raise ValueError("签核覆盖门槛须在 0 至 100% 之间。")

    grouped = defaultdict(list)
    seen = set()
    for row in rows:
        if row.get("work_date") != work_date or row.get("shift") != shift:
            continue
        validate(row)
        code = row["record_code"].strip()
        if code in seen:
            raise ValueError(f"{work_date} {shift} 的来源记录编号 {code} 重复，请核对导入台账。")
        seen.add(code)
        grouped[row["zone"]].append(row)

    by_zone = {zone: _sweep(grouped[zone]) for zone in ZONES if zone in grouped}
    eligible = sum(item.eligible for item in by_zone.values())
    if not eligible:
        raise ValueError(f"{work_date} {shift} 缺少可作业潮窗，请先录入分区潮窗。")

    covered = sum(item.covered for item in by_zone.values())
    eligible_m_min = sum(FRONTAGE_M[zone] * item.eligible for zone, item in by_zone.items())
    covered_m_min = sum(FRONTAGE_M[zone] * item.covered for zone, item in by_zone.items())
    unweighted = 100 * covered / eligible
    weighted = 100 * covered_m_min / eligible_m_min
    raw = 100 * sum(item.raw for item in by_zone.values()) / eligible
    clipped = 100 * sum(item.clipped for item in by_zone.values()) / eligible
    overlap = sum(item.overlap for item in by_zone.values())
    outside = sum(item.outside for item in by_zone.values())
    below = [zone for zone, item in by_zone.items()
             if item.eligible and 100 * item.covered / item.eligible < target]

    reasons = []
    if outside:
        reasons.append(f"先核对 {outside} 分区·分钟的窗外完工及其来源")
    if below:
        reasons.append(f"{'、'.join(below)}未达到 {target:g}% 的分区门槛，交班时列入补扫清单")
    if reasons:
        headline = "暂缓班次签核"
        action = "，".join(reasons) + "。"
    else:
        headline = "可提交班长签核"
        action = f"有潮窗的分区均达到 {target:g}% 门槛，未发现窗外完工。"
    if overlap:
        action += f"按重叠清单复核 {overlap} 分区·分钟的重复记工。"

    output = []
    for zone, item in by_zone.items():
        frontage = FRONTAGE_M[zone]
        output.append({
            "zone": zone,
            "kind": "分区汇总",
            "period": None,
            "sources": None if item.eligible else "未录入分区潮窗",
            "frontage_m": frontage,
            "eligible_min": item.eligible,
            "eligible_m_min": item.eligible * frontage,
            "raw_min": item.raw,
            "covered_min": item.covered,
            "covered_m_min": item.covered * frontage,
            "overlap_min": item.overlap,
            "outside_min": item.outside,
            "coverage_pct": round(100 * item.covered / item.eligible, 2) if item.eligible else None,
        })
        output.extend(_issue_row(zone, issue) for issue in item.issues)

    rate_points = [
        {"label": "岸线加权覆盖率", "value": round(weighted, 2)},
        {"label": "按分钟覆盖率", "value": round(unweighted, 2)},
        {"label": "原始记工率（按分钟）", "value": round(raw, 2)},
        {"label": "裁窗未去重率（按分钟）", "value": round(clipped, 2)},
    ]
    explanation = _weight_explanation(by_zone, eligible, covered, eligible_m_min, covered_m_min)
    return {
        "headline": headline,
        "summary": (
            f"{work_date} {shift}的岸线加权覆盖率为 {weighted:.2f}%"
            f"（{covered_m_min}/{eligible_m_min} 岸线米·分钟），"
            f"按分钟覆盖率为 {unweighted:.2f}%（{covered}/{eligible} 分区·分钟）。"
            f"{explanation}{action}轨迹按其登记分区的全段岸线计入，清扫面积和质量由现场复核。"
        ),
        "metrics": [
            *[{**point, "unit": "%"} for point in rate_points],
            {"label": "可作业岸线时长", "value": eligible_m_min, "unit": "岸线米·分钟"},
            {"label": "有效覆盖岸线时长", "value": covered_m_min, "unit": "岸线米·分钟"},
            {"label": "合并后潮窗", "value": eligible, "unit": "分区·分钟"},
            {"label": "窗内去重覆盖", "value": covered, "unit": "分区·分钟"},
            {"label": "窗内重复记工", "value": overlap, "unit": "分区·分钟"},
            {"label": "去重后窗外完工", "value": outside, "unit": "分区·分钟"},
        ],
        "columns": [
            {"key": "zone", "label": "海滩分区"},
            {"key": "kind", "label": "记录类型"},
            {"key": "period", "label": "本地时段"},
            {"key": "sources", "label": "来源记录编号"},
            {"key": "frontage_m", "label": "分区岸线长度（米）"},
            {"key": "eligible_min", "label": "潮窗（分钟）"},
            {"key": "covered_min", "label": "窗内去重（分钟）"},
            {"key": "eligible_m_min", "label": "可作业时长（岸线米·分钟）"},
            {"key": "covered_m_min", "label": "有效覆盖（岸线米·分钟）"},
            {"key": "raw_min", "label": "原始记工（分钟）"},
            {"key": "overlap_min", "label": "窗内重复（分钟）"},
            {"key": "outside_min", "label": "窗外去重（分钟）"},
            {"key": "coverage_pct", "label": "窗内去重覆盖率（%）"},
        ],
        "rows": output,
        "chart": {"label": "班次覆盖口径对照", "unit": "%", "points": rate_points},
    }
