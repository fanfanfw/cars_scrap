from __future__ import annotations

import logging
import os
from datetime import datetime
from pathlib import Path


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def setup_logger(service_name: str) -> logging.Logger:
    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    log_dir = repo_root() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    date = datetime.now().strftime("%Y%m%d")
    log_file = log_dir / f"{service_name}_{date}.log"

    logger = logging.getLogger(service_name)
    logger.setLevel(level)
    logger.propagate = False

    if logger.handlers:
        return logger

    formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")

    file_handler = logging.FileHandler(log_file)
    file_handler.setFormatter(formatter)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)

    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)
    return logger

