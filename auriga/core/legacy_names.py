"""Names written before the project was renamed from Daedalus to Auriga (0.2.0).

Archives, manifests, logs, queue states and frozen experiment checkouts created
under the old name stay readable; new data never uses these names.
"""

PACKAGE = "daedalus"
ENV_PREFIX = "DAEDALUS_"
MANIFEST_CONFIG_KEY = "daedalus_config"
TRAJECTORY_FORMAT = "daedalus.robodojo_trajectory.v1"
WORKER_WIRE_PREFIX = "DAEDALUS_JSON"
CONTINUITY_MARKER = "DAEDALUS_CONTINUITY_OK"
QUEUE_MODULES = ("daedalus.apps.evaluation.queue", "daedalus.evaluation.queue")
QUEUE_SCRIPT = "daedalus-queue"
TOOL_NAMESPACE = "daedalus"
