"""统一日志：控制台 + 按天滚动的文件日志，方便排查跑批异常。"""

from __future__ import annotations

import logging
import sys
from logging.handlers import TimedRotatingFileHandler

from .config import ensure_dirs, get_config

_CONFIGURED = False


def get_logger(name: str = "ecom_ops") -> logging.Logger:
    global _CONFIGURED
    if not _CONFIGURED:
        cfg = get_config()
        ensure_dirs(cfg)
        log_dir = cfg.path("paths.logs")
        log_dir.mkdir(parents=True, exist_ok=True)

        fmt = logging.Formatter(
            "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        root = logging.getLogger("ecom_ops")
        root.setLevel(logging.INFO)
        root.handlers.clear()

        stream = logging.StreamHandler(sys.stdout)
        stream.setFormatter(fmt)
        root.addHandler(stream)

        file_handler = TimedRotatingFileHandler(
            log_dir / "pipeline.log", when="midnight", backupCount=14, encoding="utf-8"
        )
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)
        _CONFIGURED = True
    return logging.getLogger(name)
