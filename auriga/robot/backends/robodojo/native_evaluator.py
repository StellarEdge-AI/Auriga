"""Host view for RoboDojo's NumPy task predicates, leaving the simulator untouched."""


def host_array(value):
    return value.detach().cpu().numpy() if hasattr(value, "detach") else value


class HostPoseView:
    """Only the evaluator gets CPU pose arrays; all other native APIs stay native."""

    def __init__(self, manager):
        self.manager = manager

    def __getattr__(self, name):
        return getattr(self.manager, name)

    def get_instance_pose(self, *args, **kwargs):
        return tuple(host_array(value) for value in self.manager.get_instance_pose(*args, **kwargs))
