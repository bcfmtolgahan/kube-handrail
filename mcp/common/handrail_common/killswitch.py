"""Remediation kill switch backed by an SSM parameter."""

import logging
import time
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

CACHE_SECONDS = 30.0


class KillSwitch:
    def __init__(
        self,
        ssm_client: Any,
        parameter_name: str,
        ttl: float = CACHE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ssm = ssm_client
        self._name = parameter_name
        self._ttl = ttl
        self._clock = clock
        self._value: bool | None = None
        self._fetched_at = 0.0

    def enabled(self) -> bool:
        now = self._clock()
        if self._value is None or now - self._fetched_at >= self._ttl:
            self._value = self._read()
            self._fetched_at = now
        return self._value

    def _read(self) -> bool:
        # Any failure to read the parameter disables remediation.
        try:
            response = self._ssm.get_parameter(Name=self._name)
        except Exception:
            logger.exception("cannot read kill switch parameter")
            return False
        return response.get("Parameter", {}).get("Value") == "true"
