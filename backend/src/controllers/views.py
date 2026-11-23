from src.utils.formatting import timestamp


def review_view(item):
    return {'decision': item.decision, 'note': item.note, 'reviewer': item.reviewer,
            'at': timestamp(item.created_at)} if item else None


def run_view(item, review=None, *, include_result=False):
    view = {'id': item.id, 'title': item.title, 'owner': item.owner, 'status': item.status,
            'parameters': item.parameters, 'sample_size': item.sample_size,
            'snapshot_id': item.last_observation_id, 'created_at': timestamp(item.created_at),
            'finished_at': timestamp(item.finished_at), 'review': review_view(review), 'error': item.error}
    if include_result:
        view['result'] = item.result
    return view


def observation_view(item):
    return {'id': item.id, 'data': item.payload, 'created_at': timestamp(item.created_at)}


def schedule_view(item):
    return {'id': item.id, 'run_id': item.run_id, 'recipient': item.recipient,
            'interval_minutes': item.interval_minutes, 'next_at': timestamp(item.next_at), 'active': item.active}


def delivery_view(delivery, schedule):
    return {'id': delivery.id, 'schedule_id': schedule.id, 'recipient': schedule.recipient,
            'run_id': schedule.run_id, 'at': timestamp(delivery.delivered_at),
            'scheduled_at': timestamp(delivery.scheduled_at)}


def correction_view(item):
    return {
        'id': item.id,
        'work_date': item.work_date,
        'shift': item.shift,
        'code': item.code,
        'submitted_by': item.submitted_by,
        'target_coverage_pct': item.target_coverage_pct,
        'issued_at': timestamp(item.issued_at),
        'segments': [
            {
                'zone': segment['zone'],
                'action': segment['action'],
                'start_minute': segment['start_minute'],
                'end_minute': segment['end_minute'],
            }
            for segment in item.segments
        ],
    }


def shift_report_view(item, *, include_result=False):
    view = {
        'id': item.id,
        'work_date': item.work_date,
        'shift': item.shift,
        'version': item.version,
        'status': item.status,
        'is_current': item.is_current,
        'watermark': item.watermark,
        'parameters': item.parameters,
        'correction_ids': list(item.correction_ids or []),
        'triggered_correction_id': item.triggered_correction_id,
        'owner': item.owner,
        'created_at': timestamp(item.created_at),
        'finished_at': timestamp(item.finished_at),
        'error': item.error,
    }
    if include_result:
        view['result'] = item.result
    return view
