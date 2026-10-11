from __future__ import annotations

import logging
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


SCRAPE_MODE = "scrape"
TRACKING_MODE = "tracking"
KUALA_LUMPUR_TZ = ZoneInfo("Asia/Kuala_Lumpur")


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def listing_log_service_name(source: str, tracking: bool = False) -> str:
    prefix = TRACKING_MODE if tracking else SCRAPE_MODE
    return f"{prefix}_{source}_cars"


def date_stamp_for(created: float) -> str:
    """YYYYMMDD stamp in Asia/Kuala_Lumpur for a POSIX timestamp (record.created)."""
    return datetime.fromtimestamp(created, tz=KUALA_LUMPUR_TZ).strftime("%Y%m%d")


class _DailyRollingFileHandler(logging.FileHandler):
    """Route each record into the daily file for its Kuala Lumpur calendar day.

    The day is part of the filename and re-derived from ``record.created`` on every
    emission, so a long-lived process rolls over to the next-day file on the first
    record of the new KL day. No rename/delete is performed.
    """

    def __init__(
        self,
        log_dir: Path,
        service_name: str,
        *,
        mode: str = "a",
        encoding: str | None = None,
    ) -> None:
        self._log_dir = log_dir
        self._service_name = service_name
        self._open_date: str | None = None
        super().__init__(os.devnull, mode=mode, encoding=encoding, delay=True)

    def _path_for(self, date_stamp: str) -> str:
        return str(self._log_dir / f"{self._service_name}_{date_stamp}.log")

    def emit(self, record: logging.LogRecord) -> None:
        try:
            date_stamp = date_stamp_for(record.created)
            if date_stamp != self._open_date:
                if self.stream is not None:
                    try:
                        self.stream.close()
                    except Exception:
                        pass
                    self.stream = None
                self.baseFilename = self._path_for(date_stamp)
                new_stream = self._open()
                self._open_date = date_stamp
                self.stream = new_stream
            super().emit(record)
        except Exception:
            self.handleError(record)


def setup_logger(service_name: str) -> logging.Logger:
    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    log_dir = repo_root() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger(service_name)
    logger.setLevel(level)
    logger.propagate = False

    if logger.handlers:
        return logger

    formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")

    file_handler = _DailyRollingFileHandler(log_dir, service_name)
    file_handler.setFormatter(formatter)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)

    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)
    return logger
