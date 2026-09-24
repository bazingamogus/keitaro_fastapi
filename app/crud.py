import json

from sqlalchemy.orm import Session

from . import models
from .keitaro_client import KeitaroClient


def sync_offers(db: Session, client: KeitaroClient) -> int:
    """Тянет полный список офферов из Keitaro и обновляет локальный кэш.
    Возвращает количество офферов после синхронизации."""
    offers = client.list_offers()
    upsert_offers(db, offers)
    return len(offers)


def upsert_offers(db: Session, offers: list[dict]):
    """Имена офферов (для автокомплита) и preview-ссылки (для редактора потоков)."""
    for o in offers:
        existing = db.get(models.Offer, o["id"])
        if existing:
            existing.name = o.get("name", "")
        else:
            db.add(models.Offer(id=o["id"], name=o.get("name", "")))
        payload = o.get("action_payload")
        url = payload if isinstance(payload, str) and payload.startswith("http") else None
        meta = db.get(models.EditorOfferMeta, o["id"])
        if meta:
            meta.preview_url = url
        else:
            db.add(models.EditorOfferMeta(id=o["id"], preview_url=url))
    db.commit()


def offer_name(db: Session, offer_id: int) -> str:
    offer = db.get(models.Offer, offer_id)
    return offer.name if offer and offer.name else f"#{offer_id}"


def offer_preview_url(db: Session, offer_id: int) -> str | None:
    meta = db.get(models.EditorOfferMeta, offer_id)
    return meta.preview_url if meta else None


def search_offers(db: Session, q: str, limit: int = 20) -> list[models.Offer]:
    query = db.query(models.Offer)
    if q:
        query = query.filter(models.Offer.name.ilike(f"%{q}%"))
    return query.order_by(models.Offer.name).limit(limit).all()


def create_log(db: Session, *, name: str, geo: str, offer_id: int) -> models.CampaignLog:
    log = models.CampaignLog(name=name, geo=geo, offer_id=offer_id, status="pending")
    db.add(log)
    db.commit()
    db.refresh(log)
    return log


def update_log(db: Session, log: models.CampaignLog, **fields) -> models.CampaignLog:
    for key, value in fields.items():
        setattr(log, key, value)
    db.commit()
    db.refresh(log)
    return log


def list_logs(db: Session, limit: int = 50) -> list[models.CampaignLog]:
    return (
        db.query(models.CampaignLog)
        .order_by(models.CampaignLog.created_at.desc())
        .limit(limit)
        .all()
    )


# --- редактор потоков: кэш кампаний и потоков ---------------------------------------------
def list_editor_campaigns(db: Session) -> list[models.EditorCampaign]:
    return db.query(models.EditorCampaign).order_by(models.EditorCampaign.id.desc()).all()


def replace_editor_campaigns(db: Session, kt_campaigns: list[dict]):
    """Список кампаний из Keitaro → editor_campaigns (исчезнувшие в Keitaro удаляются)."""
    seen = set()
    for c in kt_campaigns:
        seen.add(c["id"])
        upsert_editor_campaign(db, c)
    for row in db.query(models.EditorCampaign).all():
        if row.id not in seen:
            db.delete(row)
    db.commit()


def upsert_editor_campaign(db: Session, kt_campaign: dict) -> models.EditorCampaign:
    row = db.get(models.EditorCampaign, kt_campaign["id"]) or models.EditorCampaign(id=kt_campaign["id"])
    row.name = kt_campaign.get("name")
    row.alias = kt_campaign.get("alias")
    row.state = kt_campaign.get("state")
    row.type = kt_campaign.get("type")
    db.add(row)
    return row


def list_editor_streams(db: Session, campaign_id: int) -> list[models.EditorStream]:
    return (
        db.query(models.EditorStream)
        .filter_by(campaign_id=campaign_id)
        .order_by(models.EditorStream.position)
        .all()
    )


def upsert_editor_stream(db: Session, campaign_id: int, kt_stream: dict, stats: dict):
    """Метаданные потока + статистика {"<offer_id>": {"today": {...}, "yesterday": {...}}}."""
    row = db.get(models.EditorStream, kt_stream["id"]) or models.EditorStream(id=kt_stream["id"])
    row.campaign_id = campaign_id
    row.name = kt_stream.get("name")
    row.position = kt_stream.get("position")
    row.state = kt_stream.get("state")
    row.filters = json.dumps(
        [{"name": f.get("name"), "mode": f.get("mode"), "payload": f.get("payload")}
         for f in kt_stream.get("filters") or []],
        ensure_ascii=False,
    )
    row.clicks_today = sum((v.get("today") or {}).get("clicks", 0) for v in stats.values())
    row.stats = json.dumps(stats)
    db.add(row)


def delete_editor_streams_except(db: Session, campaign_id: int, keep_ids: set[int]):
    for row in db.query(models.EditorStream).filter_by(campaign_id=campaign_id).all():
        if row.id not in keep_ids:
            db.delete(row)


def stream_stats(db: Session, stream_id: int) -> dict:
    row = db.get(models.EditorStream, stream_id)
    return json.loads(row.stats) if row and row.stats else {}


# --- редактор потоков: черновик офферов в потоках ----------------------------------------
def stream_offers(db: Session, stream_id: int) -> list[models.StreamOffer]:
    return (
        db.query(models.StreamOffer)
        .filter_by(stream_id=stream_id)
        .order_by(models.StreamOffer.id)
        .all()
    )


def campaign_stream_offers(db: Session, campaign_id: int) -> list[models.StreamOffer]:
    return (
        db.query(models.StreamOffer)
        .filter_by(campaign_id=campaign_id)
        .order_by(models.StreamOffer.id)
        .all()
    )


def get_stream_offer(db: Session, stream_id: int, offer_id: int) -> models.StreamOffer | None:
    return db.query(models.StreamOffer).filter_by(stream_id=stream_id, offer_id=offer_id).first()


def pending_changes(db: Session, campaign_id: int) -> int:
    return sum(r.is_dirty for r in campaign_stream_offers(db, campaign_id))


def pending_changes_by_campaign(db: Session) -> dict[int, int]:
    pending: dict[int, int] = {}
    for r in db.query(models.StreamOffer).all():
        if r.is_dirty:
            pending[r.campaign_id] = pending.get(r.campaign_id, 0) + 1
    return pending


def sync_stream_offers(db: Session, campaign_id: int, kt_stream: dict):
    """Перезаписывает черновик и baseline офферов потока состоянием из Keitaro."""
    rows = {r.offer_id: r for r in stream_offers(db, kt_stream["id"])}
    in_kt = set()
    for o in kt_stream.get("offers") or []:
        in_kt.add(o["offer_id"])
        r = rows.get(o["offer_id"])
        if not r:
            r = models.StreamOffer(campaign_id=campaign_id, stream_id=kt_stream["id"],
                                   offer_id=o["offer_id"], pinned=False)
            db.add(r)
        r.kt_id = o.get("id")
        r.state = o.get("state") or "active"
        r.share = r.saved_share = o.get("share", 0)
        r.removed = r.saved_removed = False
    for offer_id, r in rows.items():
        if offer_id in in_kt:
            continue
        if r.saved_removed is None:
            db.delete(r)                  # добавлен в UI, но так и не отправлен
        else:
            r.removed = r.saved_removed = True
            r.share = r.saved_share if r.saved_share is not None else 0
    db.commit()


def mark_stream_offers_submitted(db: Session, rows: list[models.StreamOffer]):
    """Черновик отправлен в Keitaro — он становится baseline.
    Удалённые остаются в таблице с removed=True."""
    for r in rows:
        if r.removed and r.saved_removed is None:
            db.delete(r)                  # добавлен и удалён до отправки — в Keitaro его не было
        elif r.removed:
            r.saved_removed = True
        else:
            r.saved_removed = False
            r.saved_share = r.share
    db.commit()


def undo_campaign_changes(db: Session, campaign_id: int):
    """Откатывает черновик кампании к baseline (без вызовов Keitaro)."""
    for r in campaign_stream_offers(db, campaign_id):
        if r.saved_removed is None:
            db.delete(r)
        else:
            r.removed = r.saved_removed
            r.share = r.saved_share if r.saved_share is not None else 0
    db.commit()
