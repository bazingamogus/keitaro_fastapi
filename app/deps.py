"""Общие FastAPI-зависимости."""
import logging

from fastapi import HTTPException

from .config import settings
from .keitaro_client import KeitaroClient

logger = logging.getLogger("keitaro")


def get_client() -> KeitaroClient:
    if not settings.keitaro_api_key:
        logger.error("KEITARO_API_KEY не задан в .env")
        raise HTTPException(status_code=500, detail="KEITARO_API_KEY не задан в .env")
    return KeitaroClient(settings.keitaro_base_url, settings.keitaro_api_key)
