"""
Логика редактора потоков: загрузка из Keitaro в локальный кэш, локальные
правки черновика (с ребалансом share) и отправка черновика в Keitaro.

Правки делаются в черновике (таблица stream_offers) и уходят в Keitaro
только через submit_campaign(); undo откатывает черновик без вызовов API.
Кампании, потоки и статистика кэшируются в БД (editor_* таблицы): Keitaro
читается только при первом открытии и по кнопкам Fetch.
"""
import logging
import time
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from . import crud, models
from .keitaro_client import KeitaroAPIError, KeitaroClient

logger = logging.getLogger("keitaro.streams")

OFFERS_TTL = 300  # сек.: как часто при загрузке кампании перечитывать GET /offers
_offers_synced_at = 0.0


class StreamEditorError(Exception):
    """Ошибка правки черновика; status_code — какой HTTP-статус отдать."""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


# --- ребаланс share -----------------------------------------------------------------
def rebalance(rows: list[models.StreamOffer]):
    """Незакреплённые активные неудалённые офферы делят поровну то, что
    осталось от 100% после закреплённых (остаток — первым: 34/33/33)."""
    active = [r for r in rows if not r.removed and r.state == "active"]
    free = [r for r in active if not r.pinned]
    locked_sum = sum(r.share for r in active if r.pinned)
    if free:
        base, rem = divmod(max(0, 100 - locked_sum), len(free))
        for i, r in enumerate(free):
            r.share = base + (1 if i < rem else 0)
    return rows


# --- загрузка из Keitaro в локальный кэш ----------------------------------------------
def fetch_campaigns(db: Session, client: KeitaroClient):
    crud.replace_editor_campaigns(db, client.list_campaigns())


def campaigns_loaded(db: Session) -> bool:
    return db.query(models.EditorCampaign).first() is not None


def streams_loaded(db: Session, campaign_id: int) -> bool:
    camp = db.get(models.EditorCampaign, campaign_id)
    return bool(camp and camp.streams_fetched_at)


def _sync_offers_if_stale(db: Session, client: KeitaroClient, force: bool):
    global _offers_synced_at
    if force or time.time() - _offers_synced_at > OFFERS_TTL:
        crud.sync_offers(db, client)
        _offers_synced_at = time.time()


def _fetch_stats(client: KeitaroClient, campaign_id: int) -> dict[int, dict]:
    """{stream_id: {"<offer_id>": {"today": {...}, "yesterday": {...}}}}.
    Best effort: если report недоступен — пустая статистика."""
    stats: dict = {}
    for interval in ("today", "yesterday"):
        try:
            for row in client.build_report(campaign_id, interval):
                per_stream = stats.setdefault(row.get("stream_id"), {})
                per_stream.setdefault(str(row.get("offer_id")), {})[interval] = {
                    m: row.get(m) or 0 for m in ("clicks", "conversions", "revenue")
                }
        except KeitaroAPIError as e:
            logger.warning("report/build (%s) недоступен: %s", interval, e)
    return stats


def fetch_campaign(db: Session, client: KeitaroClient, campaign_id: int, refresh: bool):
    """Потоки, офферы и статистика кампании из Keitaro → локальный кэш.
    Потоки с несохранёнными правками не трогаются, если это не refresh."""
    camp = db.get(models.EditorCampaign, campaign_id)
    if not camp:  # кампания открыта по прямой ссылке и ещё не в списке
        camp = crud.upsert_editor_campaign(db, client.get_campaign(campaign_id))
    streams = [s for s in client.list_campaign_streams(campaign_id) if s.get("schema") == "landings"]
    _sync_offers_if_stale(db, client, force=refresh)
    stats = _fetch_stats(client, campaign_id)

    for s in streams:
        rows = crud.stream_offers(db, s["id"])
        if refresh or not rows or not any(r.is_dirty for r in rows):
            crud.sync_stream_offers(db, campaign_id, s)
        crud.upsert_editor_stream(db, campaign_id, s, stats.get(s["id"], {}))
    crud.delete_editor_streams_except(db, campaign_id, {s["id"] for s in streams})
    camp.streams_fetched_at = datetime.now(timezone.utc)
    db.commit()


# --- локальные правки черновика (без вызовов Keitaro) -----------------------------------
def _get_offer(db: Session, stream_id: int, offer_id: int) -> models.StreamOffer:
    r = crud.get_stream_offer(db, stream_id, offer_id)
    if not r:
        raise StreamEditorError("Оффера нет в потоке", 404)
    return r


def add_offer(db: Session, campaign_id: int, stream_id: int, offer_id: int):
    r = crud.get_stream_offer(db, stream_id, offer_id)
    if r and not r.removed:
        raise StreamEditorError("Оффер уже есть в потоке", 409)
    if r:
        r.removed = False
        if not r.pinned:
            r.share = 0
    else:
        db.add(models.StreamOffer(campaign_id=campaign_id, stream_id=stream_id, offer_id=offer_id,
                                  state="active", share=0, removed=False, pinned=False))
        db.flush()
    rebalance(crud.stream_offers(db, stream_id))
    db.commit()


def remove_offer(db: Session, stream_id: int, offer_id: int):
    _get_offer(db, stream_id, offer_id).removed = True
    rebalance(crud.stream_offers(db, stream_id))
    db.commit()


def restore_offer(db: Session, stream_id: int, offer_id: int):
    r = _get_offer(db, stream_id, offer_id)
    r.removed = False
    if not r.pinned:
        r.share = 0
    rebalance(crud.stream_offers(db, stream_id))
    db.commit()


def set_pinned(db: Session, stream_id: int, offer_id: int, pinned: bool):
    """pin/unpin — локальная настройка ребаланса, share не трогаем."""
    _get_offer(db, stream_id, offer_id).pinned = pinned
    db.commit()


def set_share(db: Session, stream_id: int, offer_id: int, share: int):
    """Ручная правка share закрепляет оффер, остальные незакреплённые делят остаток."""
    r = _get_offer(db, stream_id, offer_id)
    rows = crud.stream_offers(db, stream_id)
    locked_sum = sum(o.share for o in rows if o.pinned and o.offer_id != offer_id
                     and not o.removed and o.state == "active")
    if locked_sum + share > 100:
        raise StreamEditorError(f"Сумма закреплённых share превысит 100% ({locked_sum + share}%)")
    r.share = share
    r.pinned = True
    rebalance(rows)
    db.commit()


# --- отправка черновика в Keitaro -------------------------------------------------------
def submit_campaign(db: Session, client: KeitaroClient, campaign_id: int) -> list[int]:
    """PUT /streams/{id} для каждого потока с правками, затем GET /streams/{id}
    (актуальные kt_id). Возвращает id отправленных потоков."""
    by_stream: dict[int, list[models.StreamOffer]] = {}
    for r in crud.campaign_stream_offers(db, campaign_id):
        by_stream.setdefault(r.stream_id, []).append(r)

    submitted = []
    for stream_id, rows in by_stream.items():
        if not any(r.is_dirty for r in rows):
            continue
        offers = []
        for r in rows:
            if r.removed:
                continue
            o = {"offer_id": r.offer_id, "share": r.share, "state": r.state}
            if r.kt_id and r.saved_removed is False:
                o["id"] = r.kt_id
            offers.append(o)
        client.update_stream_offers(stream_id, offers)
        crud.mark_stream_offers_submitted(db, rows)
        crud.sync_stream_offers(db, campaign_id, client.get_stream(stream_id))
        submitted.append(stream_id)
        logger.info("stream %s: изменения отправлены в Keitaro", stream_id)
    return submitted
