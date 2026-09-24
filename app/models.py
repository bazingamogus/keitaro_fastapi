from sqlalchemy import Boolean, Column, DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.sql import func

from .database import Base


class Offer(Base):
    """Локальный кэш офферов Keitaro — нужен для быстрого автокомплита без
    похода в Keitaro API на каждое нажатие клавиши. Обновляется через
    POST /api/offers/sync."""
    __tablename__ = "offers"

    id = Column(Integer, primary_key=True)  # совпадает с id оффера в Keitaro
    name = Column(String, nullable=False, index=True)
    synced_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class CampaignLog(Base):
    """Журнал попыток создания кампаний — что просили создать, что реально
    создалось в Keitaro (или какая была ошибка)."""
    __tablename__ = "campaign_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, nullable=False)
    geo = Column(String, nullable=False)  # "MX,AU,RO"
    offer_id = Column(Integer, nullable=False)

    keitaro_campaign_id = Column(Integer, nullable=True)
    geo_redirect_stream_id = Column(Integer, nullable=True)
    offer_stream_id = Column(Integer, nullable=True)

    status = Column(String, nullable=False, default="pending")  # pending | success | error
    error = Column(Text, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())


# --- редактор потоков ------------------------------------------------------------------
class StreamOffer(Base):
    """Оффер в потоке. share/removed — черновик (то, что видно в UI),
    saved_* — последнее состояние, отправленное в/полученное из Keitaro.
    saved_removed = None — оффер добавлен в UI и ещё ни разу не отправлялся.
    Удалённые офферы не удаляются из таблицы, а помечаются removed=True.
    pinned — локальная настройка ребаланса, в Keitaro не отправляется."""
    __tablename__ = "stream_offers"
    __table_args__ = (UniqueConstraint("stream_id", "offer_id"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    campaign_id = Column(Integer, nullable=False, index=True)
    stream_id = Column(Integer, nullable=False, index=True)
    offer_id = Column(Integer, nullable=False)
    kt_id = Column(Integer, nullable=True)  # id связи stream-offer в Keitaro
    state = Column(String, nullable=False, default="active")
    pinned = Column(Boolean, nullable=False, default=False)

    share = Column(Integer, nullable=False, default=0)
    removed = Column(Boolean, nullable=False, default=False)

    saved_share = Column(Integer, nullable=True)
    saved_removed = Column(Boolean, nullable=True)

    @property
    def is_dirty(self) -> bool:
        """Есть ли несохранённые (не отправленные в Keitaro) правки."""
        if self.saved_removed is None:          # новый, ещё не отправлен
            return not self.removed
        if self.removed != self.saved_removed:
            return True
        return not self.removed and self.share != self.saved_share


class EditorCampaign(Base):
    """Кэш списка кампаний Keitaro (обновляется кнопкой Fetch campaigns)."""
    __tablename__ = "editor_campaigns"

    id = Column(Integer, primary_key=True)  # id кампании в Keitaro
    name = Column(String, nullable=True)
    alias = Column(String, nullable=True)
    state = Column(String, nullable=True)
    type = Column(String, nullable=True)
    streams_fetched_at = Column(DateTime(timezone=True), nullable=True)  # None — потоки ещё не загружались


class EditorStream(Base):
    """Кэш метаданных landing-потоков и статистики на момент загрузки."""
    __tablename__ = "editor_streams"

    id = Column(Integer, primary_key=True)  # id потока в Keitaro
    campaign_id = Column(Integer, nullable=False, index=True)
    name = Column(String, nullable=True)
    position = Column(Integer, nullable=True)
    state = Column(String, nullable=True)
    filters = Column(Text, nullable=True)  # JSON
    clicks_today = Column(Integer, nullable=False, default=0)
    stats = Column(Text, nullable=True)    # JSON: {"<offer_id>": {"today": {...}, "yesterday": {...}}}


class EditorOfferMeta(Base):
    """preview-ссылки офферов (имя берётся из таблицы offers)."""
    __tablename__ = "editor_offer_meta"

    id = Column(Integer, primary_key=True)  # id оффера в Keitaro
    preview_url = Column(String, nullable=True)
