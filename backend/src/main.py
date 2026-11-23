from contextlib import asynccontextmanager
import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import text

from scripts.seed_demo import seed_data
from src.config.project import SPEC
from src.config.runtime import SEED_DEMO
from src.middlewares.headers import response_headers
from src.middlewares.trace import trace_request
from src.models.base import Base, Session, engine
from src.routes import analysis, records, reports, session, shifts
from src.services.broker import redis
from src.services.errors import BusinessError
from src.services.shift_reports import ensure_baseline
from src.models.workflow import Observation
from sqlalchemy import select

logger = logging.getLogger('uvicorn.error')


@asynccontextmanager
async def lifespan(_app):
    Base.metadata.create_all(engine)
    if SEED_DEMO:
        try:
            logger.info('demo row counts %s', seed_data())
        except Exception:
            logger.exception('demo seed failed')
    try:
        ensure_demo_reports()
    except Exception:
        logger.exception('demo shift report bootstrap failed')
    yield


def ensure_demo_reports():
    """Queue a correction-free baseline report for every seeded shift."""

    with Session.begin() as db:
        shifts = {
            (row.payload.get('work_date'), row.payload.get('shift'))
            for row in db.execute(select(Observation)).scalars()
        }
        for work_date, shift in sorted(shifts):
            if work_date and shift:
                ensure_baseline(db, work_date, shift)


app = FastAPI(title=SPEC['title'], lifespan=lifespan)
app.middleware('http')(trace_request)
app.middleware('http')(response_headers)
for module in (session, records, analysis, reports, shifts):
    app.include_router(module.router)


@app.exception_handler(BusinessError)
async def business_error(_request: Request, exc: BusinessError):
    return JSONResponse({'detail': str(exc)}, status_code=exc.status)


@app.exception_handler(RequestValidationError)
async def request_error(_request: Request, exc: RequestValidationError):
    first = exc.errors()[0]
    label = str(first['loc'][-1])
    return JSONResponse({'detail': f'请求字段不符合要求（{label}）'}, status_code=422)


@app.get('/api/health')
def health():
    with Session() as database:
        database.execute(text('SELECT 1'))
    redis.ping()
    return {'status': 'ok', 'project': SPEC['slug']}
