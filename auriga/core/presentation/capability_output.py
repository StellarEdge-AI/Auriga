"""Reuse immutable robot capabilities within one model context.

Live execution budgets remain explicit. Programs and explicit full reads do not
consume the model's delivery baseline, and any changed capability is resent.
"""

from copy import deepcopy

from ..contracts import digest


class CapabilityDelivery:
    def __init__(self):
        self._epoch = None
        self._version = None

    def project(self, capabilities, *, epoch, current_action_budget):
        version = digest(capabilities)
        repeated = self._epoch == epoch and self._version == version
        result = {} if repeated else deepcopy(capabilities)
        result["capabilities_delivery"] = dict(
            version=version, status="unchanged" if repeated else "inline",
            scope="current_model_context",
            read_full=dict(tool="robot_capabilities", detail="full"))
        result["current_action_budget"] = deepcopy(current_action_budget)
        self._epoch, self._version = deepcopy(epoch), version
        return result
