"""Настройка логирования приложения в файл (с ротацией) + консоль."""
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import settings

LOGGER_NAME = "keitaro"
_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"


def setup_logging() -> logging.Logger:
    logger = logging.getLogger(LOGGER_NAME)
    # uvicorn --reload может импортировать модуль повторно — не дублируем хендлеры
    if logger.handlers:
        return logger

    log_path = Path(settings.log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    file_handler = RotatingFileHandler(
        log_path, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(logging.Formatter(_FORMAT))

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(logging.Formatter(_FORMAT))

    logger.setLevel(settings.log_level.upper())
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    logger.propagate = False
    return logger
