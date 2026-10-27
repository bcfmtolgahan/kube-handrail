"""Remediation kill switch backed by an SSM parameter."""

import logging
from typing import Any

logger = logging.getLogger(__name__)


class KillSwitch:
    """Reads the parameter on every check, so turning it off takes effect immediately."""

    def __init__(self, ssm_client: Any, parameter_name: str) -> None:
        self._ssm = ssm_client
        self._name = parameter_name

    def enabled(self) -> bool:
        # Any failure to read the parameter disables remediation.
        try:
            response = self._ssm.get_parameter(Name=self._name)
        except Exception:
            logger.exception("cannot read kill switch parameter")
            return False
        return response.get("Parameter", {}).get("Value") == "true"
