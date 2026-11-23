from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response

from src.config.project import SPEC
from src.constants.enums import Role, VersionStatus
from src.controllers.views import correction_view, shift_report_view
from src.models.base import Session
from src.schemas.requests import CorrectionInput
from src.services.auth import current_user, roles
from src.services.broker import wake_worker
from src.services.errors import BusinessError
from src.services.shift_reports import (
    corrections_by_ids,
    corrections_for_shift,
    current_report,
    reports_for_shift,
    retry_report,
    submit_correction,
    visible_reports,
)
from src.utils.formatting import csv_text

router = APIRouter(prefix='/api')
editor = roles(Role.SUPERVISOR, Role.ANALYST)


def _detail(db, report, *, include_result=True):
    view = shift_report_view(report, include_result=include_result)
    view['corrections'] = [correction_view(item) for item in corrections_by_ids(db, list(report.correction_ids or []))]
    return view


def _resolve(db, user, report_id):
    return visible_reports(db, user, report_id)


@router.post('/corrections', status_code=201)
def create_correction(request: CorrectionInput, user=Depends(editor)):
    with Session.begin() as db:
        correction, report = submit_correction(db, request.model_dump(), user['sub'])
        view = {'correction': correction_view(correction), 'report': shift_report_view(report)}
    wake_worker()
    return view


@router.get('/corrections')
def list_corrections(work_date: str = Query(...), shift: str = Query(...), user=Depends(current_user)):
    with Session() as db:
        return [correction_view(item) for item in corrections_for_shift(db, work_date, shift)]


@router.get('/shift-reports')
def shift_index(user=Depends(current_user)):
    with Session() as db:
        reports = visible_reports(db, user)
        buckets = {}
        for report in reports:
            key = (report.work_date, report.shift)
            entry = buckets.setdefault(key, {'work_date': report.work_date, 'shift': report.shift,
                                             'current': None, 'latest_version': 0, 'versions': 0})
            entry['versions'] += 1
            entry['latest_version'] = max(entry['latest_version'], report.version)
            if report.is_current:
                entry['current'] = shift_report_view(report)
        return list(buckets.values())


@router.get('/shift-reports/history')
def shift_history(work_date: str = Query(...), shift: str = Query(...), user=Depends(current_user)):
    with Session() as db:
        reports = reports_for_shift(db, work_date, shift)
        allowed = visible_reports(db, user)
        visible_ids = {item.id for item in allowed}
        current = current_report(db, work_date, shift)
        return {
            'current': shift_report_view(current) if current and current.id in visible_ids else None,
            'versions': [shift_report_view(item) for item in reports if item.id in visible_ids],
            'corrections': [correction_view(item) for item in corrections_for_shift(db, work_date, shift)],
        }


@router.get('/shift-reports/current')
def shift_current(work_date: str = Query(...), shift: str = Query(...), user=Depends(current_user)):
    with Session() as db:
        report = current_report(db, work_date, shift)
        if report is None:
            raise BusinessError('该班次还没有班报版本', 404)
        if user['role'] == Role.VIEWER and report.status != VersionStatus.SUCCEEDED:
            raise BusinessError('该班次当前版本正在补算，请稍后查看', 409)
        return _detail(db, report)


@router.get('/shift-reports/{report_id}')
def shift_detail(report_id: int, user=Depends(current_user)):
    with Session() as db:
        return _detail(db, _resolve(db, user, report_id))


@router.get('/shift-reports/{report_id}/export')
def shift_export(report_id: int, user=Depends(current_user)):
    with Session() as db:
        report = _resolve(db, user, report_id)
        if report.status != VersionStatus.SUCCEEDED or not report.result:
            raise BusinessError('班报算完后才能下载明细', 409)
        file = f'{SPEC["slug"]}-shift-{report.work_date}-{report.id}-v{report.version}.csv'
        return Response(csv_text(report.result['columns'], report.result['rows']), media_type='text/csv',
                        headers={'Content-Disposition': f'attachment; filename={file}'})


@router.post('/shift-reports/{report_id}/retry', status_code=201)
def shift_retry(report_id: int, user=Depends(editor)):
    with Session.begin() as db:
        row = retry_report(db, report_id, user['sub'])
        view = shift_report_view(row)
    wake_worker()
    return view
