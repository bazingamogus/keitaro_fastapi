"""
Роуты редактора потоков: страница /streams и API /api/editor/*.
Логика — в streams_service.py, работа с БД — в crud.py, таблицы — в models.py.
"""
import json
import logging
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from . import crud, models, schemas, streams_service as service
from .config import settings
from .database import get_db
from .deps import get_client
from .keitaro_client import KeitaroAPIError, KeitaroClient

logger = logging.getLogger("keitaro.streams")
STATIC_DIR = Path(__file__).resolve().parent / "static"

router = APIRouter()


@contextmanager
def _errors():
    """KeitaroAPIError → 502, StreamEditorError → его статус."""
    try:
        yield
    except KeitaroAPIError as e:
        logger.error("Keitaro error: %s", e)
        raise HTTPException(status_code=502, detail=str(e))
    except service.StreamEditorError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))


# --- формирование ответов -------------------------------------------------------------
def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).isoformat()  # SQLite теряет tz


def _admin_url(campaign_id: int) -> str:
    root = settings.keitaro_base_url.split("/admin_api")[0].rstrip("/")
    return f"{root}/admin/#!/campaigns/{campaign_id}"


def _offer_out(db: Session, r: models.StreamOffer, stats: dict) -> dict:
    st = stats.get(str(r.offer_id), {})
    return {
        "offer_id": r.offer_id,
        "name": crud.offer_name(db, r.offer_id),
        "preview_url": crud.offer_preview_url(db, r.offer_id),
        "share": r.share,
        "saved_share": r.saved_share,
        "state": r.state,
        "pinned": r.pinned,
        "removed": r.removed,
        "added": r.saved_removed is None,
        "changed": r.is_dirty,
        "today": st.get("today"),
        "yesterday": st.get("yesterday"),
    }


def _stream_offers_out(db: Session, stream_id: int) -> list[dict]:
    stats = crud.stream_stats(db, stream_id)
    return [_offer_out(db, r, stats) for r in crud.stream_offers(db, stream_id)]


def _stream_state(db: Session, stream_id: int) -> dict:
    """Ответ на локальные правки: офферы потока + счётчик несохранённых правок."""
    rows = crud.stream_offers(db, stream_id)
    if not rows:
        raise HTTPException(status_code=404, detail="Поток не загружен — откройте кампанию заново")
    return {"offers": _stream_offers_out(db, stream_id),
            "pending_changes": crud.pending_changes(db, rows[0].campaign_id)}


# --- страницы --------------------------------------------------------------------------
@router.get("/streams", include_in_schema=False)
@router.get("/streams/{campaign_id}", include_in_schema=False)
def streams_page(campaign_id: int | None = None):
    return FileResponse(STATIC_DIR / "streams.html")


# --- API: чтение (из локального кэша; Keitaro — только в первый раз и по refresh) -------
@router.get("/api/editor/campaigns")
def list_campaigns(refresh: bool = False, db: Session = Depends(get_db),
                   client: KeitaroClient = Depends(get_client)):
    if refresh or not service.campaigns_loaded(db):
        with _errors():
            service.fetch_campaigns(db, client)
    pending = crud.pending_changes_by_campaign(db)
    return [
        {"id": c.id, "name": c.name, "alias": c.alias, "state": c.state, "type": c.type,
         "pending_changes": pending.get(c.id, 0)}
        for c in crud.list_editor_campaigns(db)
    ]


@router.get("/api/editor/campaigns/{campaign_id}")
def campaign_streams(
    campaign_id: int,
    refresh: bool = False,
    db: Session = Depends(get_db),
    client: KeitaroClient = Depends(get_client),
):
    if refresh or not service.streams_loaded(db, campaign_id):
        with _errors():
            service.fetch_campaign(db, client, campaign_id, refresh)
    camp = db.get(models.EditorCampaign, campaign_id)

    streams = [
        {
            "id": s.id,
            "name": s.name,
            "position": s.position,
            "state": s.state,
            "filters": json.loads(s.filters) if s.filters else [],
            "clicks_today": s.clicks_today,
            "offers": _stream_offers_out(db, s.id),
        }
        for s in crud.list_editor_streams(db, campaign_id)
    ]
    return {
        "campaign": {"id": camp.id, "name": camp.name, "admin_url": _admin_url(campaign_id),
                     "fetched_at": _iso(camp.streams_fetched_at)},
        "streams": streams,
        "pending_changes": crud.pending_changes(db, campaign_id),
    }


# --- API: локальные правки (без вызовов Keitaro) ----------------------------------------
@router.post("/api/editor/streams/{stream_id}/offers")
def add_offer(stream_id: int, body: schemas.StreamOfferAdd, db: Session = Depends(get_db)):
    with _errors():
        service.add_offer(db, body.campaign_id, stream_id, body.offer_id)
    return _stream_state(db, stream_id)


@router.delete("/api/editor/streams/{stream_id}/offers/{offer_id}")
def remove_offer(stream_id: int, offer_id: int, db: Session = Depends(get_db)):
    with _errors():
        service.remove_offer(db, stream_id, offer_id)
    return _stream_state(db, stream_id)


@router.post("/api/editor/streams/{stream_id}/offers/{offer_id}/restore")
def restore_offer(stream_id: int, offer_id: int, db: Session = Depends(get_db)):
    with _errors():
        service.restore_offer(db, stream_id, offer_id)
    return _stream_state(db, stream_id)


@router.patch("/api/editor/streams/{stream_id}/offers/{offer_id}")
def update_offer(stream_id: int, offer_id: int, body: schemas.StreamOfferUpdate,
                 db: Session = Depends(get_db)):
    with _errors():
        if body.share is not None:
            service.set_share(db, stream_id, offer_id, body.share)
        elif body.pinned is not None:
            service.set_pinned(db, stream_id, offer_id, body.pinned)
    return _stream_state(db, stream_id)


@router.post("/api/editor/campaigns/{campaign_id}/undo")
def undo_changes(campaign_id: int, db: Session = Depends(get_db)):
    crud.undo_campaign_changes(db, campaign_id)
    stream_ids = {r.stream_id for r in crud.campaign_stream_offers(db, campaign_id)}
    return {"streams": {sid: _stream_offers_out(db, sid) for sid in stream_ids}, "pending_changes": 0}


# --- API: отправка в Keitaro ------------------------------------------------------------
@router.post("/api/editor/campaigns/{campaign_id}/submit")
def submit_changes(campaign_id: int, db: Session = Depends(get_db),
                   client: KeitaroClient = Depends(get_client)):
    with _errors():
        submitted = service.submit_campaign(db, client, campaign_id)
    return {"submitted_streams": submitted, "pending_changes": crud.pending_changes(db, campaign_id)}
