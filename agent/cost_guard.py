"""Daily Gemini request budget enforcer.

Resets at midnight UTC. Thread-safe. Logs a warning at 80% consumption.
"""

from __future__ import annotations

import logging
import os
import threading
from datetime import UTC, datetime

logger = logging.getLogger(__name__)

_DEFAULT_DAILY_LIMIT = 200


class CostGuard:
    def __init__(self, daily_limit: int | None = None) -> None:
        self._limit = daily_limit or int(
            os.environ.get("DAILY_REQUEST_LIMIT", str(_DEFAULT_DAILY_LIMIT))
        )
        self._lock = threading.Lock()
        self._count = 0
        self._day = self._today()
        self._warned = False

    @staticmethod
    def _today() -> str:
        return datetime.now(UTC).strftime("%Y-%m-%d")

    def _reset_if_new_day(self) -> None:
        today = self._today()
        if today != self._day:
            self._day = today
            self._count = 0
            self._warned = False

    def check_and_increment(self) -> bool:
        with self._lock:
            self._reset_if_new_day()
            if self._count >= self._limit:
                logger.error(
                    "Daily Gemini budget exhausted (%d/%d)", self._count, self._limit
                )
                return False
            self._count += 1
            if not self._warned and self._count >= int(self._limit * 0.8):
                logger.warning(
                    "Gemini budget at 80%%: %d/%d requests used today",
                    self._count,
                    self._limit,
                )
                self._warned = True
            return True

    def remaining(self) -> int:
        with self._lock:
            self._reset_if_new_day()
            return max(0, self._limit - self._count)

    def used(self) -> int:
        with self._lock:
            self._reset_if_new_day()
            return self._count
