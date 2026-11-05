"""Remediation kill switch backed by an SSM parameter."""

import logging
from typing import Any

logger = logging.getLogger(__name__)


class KillSwitch:
    """Reads the parameter on every check, so turning it off takes effect immediately."""

    def __init__(self, ssm_client: Any, parameter_name: str) -> None:
        self._ssm = ssm_client
        self._name = parameter_name

    def state(self) -> bool | None:
        """True only for the exact value "true", False for any other value, and None when
        the parameter cannot be read. A revert tells an unreadable switch from one turned
        off: it retries the first and gives up on the second."""
        try:
            response = self._ssm.get_parameter(Name=self._name)
        except Exception:
            logger.exception("cannot read kill switch parameter")
            return None
        return response.get("Parameter", {}).get("Value") == "true"

    def enabled(self) -> bool:
        # Any failure to read the parameter disables remediation.
        return self.state() is True
