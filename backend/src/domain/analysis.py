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

WORKABLE = "可作业"
CLOSED = "禁作业"
UNDECLARED = "未公告"
WINDOW_ACTIONS = (WORKABLE, CLOSED)
MINUTES_PER_DAY = 1440

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


def _sweep_tracks(tracks: list[dict], windows: list[tuple[int, int, str]]) -> ZoneCoverage:
    """Sweep completion tracks against already-resolved windows.

    ``tracks`` are ``{record_code, start_minute, end_minute}`` rows; ``windows``
    are ``(start, end, label)`` workable intervals where ``label`` is used as the
    source on gaps. Reporting matches the bulletin sweep: outside-work is the
    track union outside every window, overlap is repeated in-window minutes,
    and gaps are workable minutes with no track.
    """

    events = defaultdict(list)
    boundaries = set()
    for start, end, _label in windows:
        events[start].append((WINDOW, None, True))
        events[end].append((WINDOW, None, False))
        boundaries.update((start, end))
    for row in tracks:
        code = row["record_code"].strip()
        events[int(row["start_minute"])].append((TRACK, code, True))
        events[int(row["end_minute"])].append((TRACK, code, False))
        boundaries.update((int(row["start_minute"]), int(row["end_minute"])))

    coverage = ZoneCoverage()
    active = {WINDOW: 0, TRACK: set()}
    previous = 0
    for minute in sorted(boundaries):
        span = minute - previous
        count = len(active[TRACK])
        coverage.raw += count * span
        if active[WINDOW]:
            coverage.eligible += span
            coverage.clipped += count * span
            if active[TRACK]:
                coverage.covered += span
                if count > 1:
                    coverage.add_issue(OVERLAP, previous, minute, active[TRACK])
            else:
                coverage.add_issue(GAP, previous, minute, {_window_label(windows, previous, minute)})
        elif active[TRACK]:
            coverage.outside += span
            coverage.add_issue(OUTSIDE, previous, minute, active[TRACK])

        for kind, code, opens in events[minute]:
            if kind == WINDOW:
                active[WINDOW] += 1 if opens else -1
            elif opens:
                active[TRACK].add(code)
            else:
                active[TRACK].discard(code)
        previous = minute
    return coverage


def _window_label(windows: list[tuple[int, int, str]], start: int, end: int) -> str:
    labels = sorted({label for low, high, label in windows if low <= start and end <= high})
    return "、".join(labels) if labels else WINDOW



def _clock(minute: int) -> str:
    return f"{minute // 60:02d}:{minute % 60:02d}"


def normalize_correction(payload: dict) -> dict:
    """Validate and normalize a tide-window correction payload.

    A correction describes one work date + shift and a list of segments
    ``{zone, action, start_minute, end_minute}`` where ``action`` is either
    workable or closed. At least one segment is required. Segments may touch
    but must not overlap within the same zone and action pair; open/closed
    overlaps on the same zone are allowed because the latest correction wins.
    """

    work_date = payload.get("work_date")
    _validate_date(work_date)
    shift = payload.get("shift")
    if shift not in SHIFTS:
        raise ValueError("请选择早班或下午班。")
    code = payload.get("code")
    if not isinstance(code, str) or not code.strip():
        raise ValueError("潮窗更正编号不能为空。")
    code = code.strip()
    if len(code) > 96:
        raise ValueError("潮窗更正编号过长。")

    segments = payload.get("segments")
    if not isinstance(segments, list) or not segments:
        raise ValueError("潮窗更正须至少包含一个分区时段。")
    if len(segments) > 200:
        raise ValueError("单次潮窗更正的分区时段过多。")

    clean = []
    for index, segment in enumerate(segments, 1):
        if not isinstance(segment, dict):
            raise ValueError(f"第 {index} 段潮窗格式不正确。")
        zone = segment.get("zone")
        if zone not in ZONES:
            raise ValueError(f"第 {index} 段潮窗的分区无效。")
        action = segment.get("action", WORKABLE)
        if action not in WINDOW_ACTIONS:
            raise ValueError(f"第 {index} 段潮窗须标注可作业或禁作业。")
        start, end = segment.get("start_minute"), segment.get("end_minute")
        for point in (start, end):
            if type(point) not in (int, float) or not 0 <= point <= MINUTES_PER_DAY or int(point) != point:
                raise ValueError(f"第 {index} 段潮窗的时间须在 0 至 1440 之间的整分钟。")
        if start >= end:
            raise ValueError(f"第 {index} 段潮窗的结束分钟必须晚于开始分钟。")
        if segment.get("frontage_m") is not None:
            frontage = segment["frontage_m"]
            if type(frontage) not in (int, float) or frontage != FRONTAGE_M[zone]:
                raise ValueError(f"第 {index} 段潮窗的{zone}岸线长度须与分区台账一致（{FRONTAGE_M[zone]} 米）。")
        clean.append({
            "zone": zone,
            "action": action,
            "start_minute": int(start),
            "end_minute": int(end),
        })

    _reject_same_action_overlap(clean)
    target = payload.get("target_coverage_pct", DEFAULT_TARGET)
    if type(target) not in (int, float) or not 0 <= target <= 100:
        raise ValueError("签核覆盖门槛须在 0 至 100% 之间。")
    return {"work_date": work_date, "shift": shift, "code": code, "segments": clean,
            "target_coverage_pct": target}


def _reject_same_action_overlap(segments: list[dict]) -> None:
    grouped = defaultdict(list)
    for segment in segments:
        grouped[(segment["zone"], segment["action"])].append(segment)
    for (zone, action), items in grouped.items():
        items.sort(key=lambda item: item["start_minute"])
        for left, right in zip(items, items[1:], strict=False):
            if right["start_minute"] < left["end_minute"]:
                raise ValueError(
                    f"{zone}的{action}时段 {_clock(left['start_minute'])}–{_clock(left['end_minute'])} "
                    f"与 {_clock(right['start_minute'])}–{_clock(right['end_minute'])} 重叠，请合并后重交。"
                )


def correction_interval(segment: dict) -> tuple[int, int]:
    return int(segment["start_minute"]), int(segment["end_minute"])


def resolve_windows(
    shift_windows: dict[str, list[dict]],
    corrections: list[dict],
) -> tuple[dict[str, list[tuple[int, int]]], dict[str, list[dict]]]:
    """Resolve effective workable windows per zone after applying corrections.

    ``shift_windows`` maps each zone to the original bulletin segments
    ``{record_code, start_minute, end_minute}``. ``corrections`` is a list of
    ``{id, code, issued_at, segments}`` where each segment is
    ``{zone, action, start_minute, end_minute}``.

    For every minute, the latest correction that declares the zone wins
    (``issued_at`` ties broken by ``id``); its action (workable / closed)
    applies. Minutes no correction mentions fall back to the original
    bulletin. Nothing is dropped: a correction that only changes part of a
    zone leaves the untouched bulletin minutes intact, and a correction that
    only mentions some zones leaves other zones' bulletins intact.

    Returns ``(effective_windows, window_rows)`` where ``effective_windows`` is
    a zone→list of ``(start, end)`` workable intervals and ``window_rows`` is
    the auditable per-zone breakdown of every resolved segment with its source.
    """

    ordered_corrections = sorted(corrections, key=_correction_rank)
    effective: dict[str, list[tuple[int, int]]] = {}
    window_rows: dict[str, list[dict]] = {}
    for zone in ZONES:
        bulletin = sorted(
            (int(item["start_minute"]), int(item["end_minute"]))
            for item in shift_windows.get(zone, [])
        )
        relevant = [item for item in ordered_corrections
                    if any(seg["zone"] == zone for seg in item["segments"])]
        if zone not in shift_windows and not relevant:
            continue

        boundaries = {point for start, end in bulletin for point in (start, end)}
        for item in relevant:
            for seg in item["segments"]:
                if seg["zone"] == zone:
                    boundaries.update(correction_interval(seg))
        ordered = sorted(boundaries)

        segments: list[dict] = []
        for start, end in zip(ordered, ordered[1:], strict=False):
            if start >= end:
                continue
            action, source = _action_at(zone, start, bulletin, relevant)
            _append_segment(segments, zone, action, source, start, end)

        effective[zone] = [
            (row["start_minute"], row["end_minute"])
            for row in segments if row["action"] == WORKABLE
        ]
        window_rows[zone] = segments
    return effective, window_rows


def _append_segment(segments: list[dict], zone: str, action: str, source: dict | None,
                    start: int, end: int) -> None:
    row = {
        "zone": zone,
        "action": action,
        "source": "潮窗更正" if source else "原班报潮窗",
        "source_code": source["code"] if source else None,
        "correction_id": source["id"] if source else None,
        "start_minute": start,
        "end_minute": end,
    }
    if segments:
        last = segments[-1]
        if (last["action"] == action and last["correction_id"] == row["correction_id"]
                and last["end_minute"] == start):
            last["end_minute"] = end
            return
    segments.append(row)


def _action_at(zone: str, minute: int, bulletin: list[tuple[int, int]],
               corrections: list[dict]) -> tuple[str, dict | None]:
    """Return the effective action and its source correction at ``minute``.

    ``corrections`` must be sorted oldest-first; the last correction whose
    segment covers ``minute`` for ``zone`` wins, so later corrections
    supersede earlier ones on overlapping minutes.
    """

    latest = None
    for item in corrections:
        for seg in item["segments"]:
            if seg["zone"] != zone:
                continue
            start, end = correction_interval(seg)
            if start <= minute < end:
                latest = {"item": item, "segment": seg}
    if latest:
        return latest["segment"]["action"], latest["item"]
    inside = any(start <= minute < end for start, end in bulletin)
    return (WORKABLE if inside else UNDECLARED), None


def _correction_rank(item: dict) -> tuple:
    issued = item.get("issued_at")
    return (issued or "", item.get("id") or 0)


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


def _collect_shift(rows: list[dict], work_date: str, shift: str) -> dict[str, list[dict]]:
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
    return grouped


def analyze_shift(rows: list[dict], corrections: list[dict], parameters: dict) -> dict:
    """Build a versioned shift report from the original tracks plus corrections.

    ``rows`` is the full observation snapshot for the shift, including the
    original tide-window bulletin rows (``record_kind == 可作业潮窗``) and the
    completion tracks. Completion tracks are always taken from this original
    snapshot; corrections only change which minutes are workable.

    ``corrections`` is ``[{id, code, issued_at, segments}]`` ordered freely;
    overlap resolution is by issue time, so submitting corrections out of
    order still yields the latest-signed correction winning each minute.
    """

    work_date = parameters.get("work_date", DEFAULT_DATE)
    shift = parameters.get("shift", DEFAULT_SHIFT)
    target = parameters.get("target_coverage_pct", DEFAULT_TARGET)
    _validate_date(work_date)
    if shift not in SHIFTS:
        raise ValueError("请选择要复核的早班或下午班。")
    if type(target) not in (int, float) or not 0 <= target <= 100:
        raise ValueError("签核覆盖门槛须在 0 至 100% 之间。")

    grouped = _collect_shift(rows, work_date, shift)
    if not grouped and not corrections:
        raise ValueError(f"{work_date} {shift} 缺少完工轨迹与潮窗，请先录入台账。")

    bulletin: dict[str, list[dict]] = {}
    tracks: dict[str, list[dict]] = {}
    for zone, items in grouped.items():
        bulletin[zone] = [row for row in items if row["record_kind"] == WINDOW]
        tracks[zone] = [row for row in items if row["record_kind"] == TRACK]

    effective, window_detail = resolve_windows(bulletin, corrections)

    zones = [zone for zone in ZONES
             if zone in grouped or zone in effective or zone in window_detail]
    by_zone = {}
    for zone in zones:
        workable = [row for row in window_detail.get(zone, []) if row["action"] == WORKABLE]
        windows = [(row["start_minute"], row["end_minute"], row["source_code"] or WINDOW)
                   for row in workable]
        by_zone[zone] = _sweep_tracks(tracks.get(zone, []), windows)

    eligible = sum(item.eligible for item in by_zone.values())
    covered = sum(item.covered for item in by_zone.values())
    eligible_m_min = sum(FRONTAGE_M[zone] * item.eligible for zone, item in by_zone.items())
    covered_m_min = sum(FRONTAGE_M[zone] * item.covered for zone, item in by_zone.items())
    raw = sum(item.raw for item in by_zone.values())
    clipped = sum(item.clipped for item in by_zone.values())
    overlap = sum(item.overlap for item in by_zone.values())
    outside = sum(item.outside for item in by_zone.values())

    unweighted = 100 * covered / eligible if eligible else None
    weighted = 100 * covered_m_min / eligible_m_min if eligible_m_min else None
    raw_rate = 100 * raw / eligible if eligible else None
    clipped_rate = 100 * clipped / eligible if eligible else None

    below = [zone for zone, item in by_zone.items()
             if item.eligible and 100 * item.covered / item.eligible < target]

    reasons = []
    if not eligible:
        reasons.append("本班没有生效的可作业潮窗，请核对潮窗更正或补录台账")
    if outside:
        if eligible:
            reasons.append(f"先核对 {outside} 分区·分钟的窗外完工及其来源")
        else:
            reasons.append(f"另有 {outside} 分区·分钟的完工落在可作业潮窗之外，已逐段列出以便交班核对")
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
            "sources": None if item.eligible else "未录入可作业潮窗",
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
        {"label": label, "value": None if value is None else round(value, 2)}
        for label, value in (
            ("岸线加权覆盖率", weighted),
            ("按分钟覆盖率", unweighted),
            ("原始记工率（按分钟）", raw_rate),
            ("裁窗未去重率（按分钟）", clipped_rate),
        )
    ]
    if eligible_m_min:
        explanation = _weight_explanation(by_zone, eligible, covered, eligible_m_min, covered_m_min)
        weighted_text = (f"岸线加权覆盖率为 {weighted:.2f}%"
                         f"（{covered_m_min}/{eligible_m_min} 岸线米·分钟），")
        unweighted_text = f"按分钟覆盖率为 {unweighted:.2f}%（{covered}/{eligible} 分区·分钟）。"
    else:
        explanation = "本班没有可作业潮窗，覆盖率分母为 0，暂不计算覆盖率。"
        weighted_text = "岸线加权覆盖率无法计算（无可作业潮窗），"
        unweighted_text = "按分钟覆盖率无法计算（可作业 0 分区·分钟）。"

    window_columns = [
        {"key": "zone", "label": "海滩分区"},
        {"key": "source", "label": "来源类型"},
        {"key": "source_code", "label": "来源记录编号"},
        {"key": "action", "label": "区间性质"},
        {"key": "period", "label": "本地时段"},
        {"key": "duration_min", "label": "时长（分钟）"},
        {"key": "frontage_m", "label": "分区岸线长度（米）"},
        {"key": "m_min", "label": "岸线米·分钟"},
    ]
    window_rows = []
    for zone in zones:
        for row in window_detail.get(zone, []):
            if row["action"] == UNDECLARED:
                continue
            duration = row["end_minute"] - row["start_minute"]
            if duration <= 0:
                continue
            window_rows.append({
                "zone": zone,
                "source": row["source"],
                "source_code": row["source_code"] or "—",
                "action": row["action"],
                "period": f"{_clock(row['start_minute'])}–{_clock(row['end_minute'])}",
                "duration_min": duration,
                "frontage_m": FRONTAGE_M[zone],
                "m_min": duration * FRONTAGE_M[zone],
            })

    return {
        "headline": headline,
        "summary": (
            f"{work_date} {shift}的{weighted_text}{unweighted_text}"
            f"{explanation}{action}轨迹按其登记分区的全段岸线计入，清扫面积和质量由现场复核。"
        ),
        "metrics": [
            {"label": "岸线加权覆盖率", "value": None if weighted is None else round(weighted, 2), "unit": "%"},
            {"label": "按分钟覆盖率", "value": None if unweighted is None else round(unweighted, 2), "unit": "%"},
            {"label": "原始记工率（按分钟）", "value": None if raw_rate is None else round(raw_rate, 2), "unit": "%"},
            {"label": "裁窗未去重率（按分钟）", "value": None if clipped_rate is None else round(clipped_rate, 2), "unit": "%"},
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
        "windows": {"columns": window_columns, "rows": window_rows},
        "chart": {"label": "班次覆盖口径对照", "unit": "%", "points": rate_points},
    }
