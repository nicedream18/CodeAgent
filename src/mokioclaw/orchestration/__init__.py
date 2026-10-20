"""CrewAI Flow orchestration with the existing controlled Agent execution layer."""

import os
import tempfile
from importlib.metadata import version

# Keep local observability authoritative before CrewAI initializes listeners.
os.environ["CREWAI_DISABLE_TELEMETRY"] = "true"
os.environ["CREWAI_TRACING_ENABLED"] = "false"

# CrewAI imports its default storage constants even when memory is disabled.
# Keep that empty scaffold private and ephemeral instead of touching a user's
# unrelated CrewAI application-data directory on import.
_storage = None
if "CREWAI_STORAGE_DIR" not in os.environ:
    _storage = tempfile.TemporaryDirectory(prefix="mokio-crewai-runtime-")
    os.environ["CREWAI_STORAGE_DIR"] = _storage.name

BACKEND = "crewai"
FRAMEWORK_VERSION = version("crewai")
