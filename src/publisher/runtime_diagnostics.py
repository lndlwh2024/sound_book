"""Public application identity for upgrade and diagnostic checks; no auth state."""
import os
import sys
from datetime import datetime
from pathlib import Path
from .event_log import CHINA_TIME
from ..version import __version__


def runtime_identity():
    executable=str(Path(sys.executable).resolve())
    entrypoint=executable if getattr(sys,'frozen',False) else str(Path(sys.argv[0]).resolve())
    return {'schema':1,'version':__version__,'pid':os.getpid(),'executable':executable,
            'entrypoint':entrypoint,'captured_at':datetime.now(CHINA_TIME).isoformat()}
