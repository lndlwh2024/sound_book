"""Run diagnostic serialization and compression outside the Qt event thread."""
from copy import deepcopy
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from .diagnostic_export import ExportCancelled, export_diagnostics
from .event_log import redact


class DiagnosticExportWorker(QThread):
    progress = Signal(str)

    def __init__(self, root, log_folder, destination, *, task_id=None, cutoff=None,
                 cloud_seed=None, credentials=None, watcher=None, parent=None):
        super().__init__(parent)
        self.root, self.log_folder, self.destination = map(Path,(root,log_folder,destination))
        self.task_id, self.cutoff = task_id, cutoff
        # Only small configuration values are copied by the GUI. Credential
        # reads, watcher locks and all on-disk context are handled in run().
        self.cloud_seed = deepcopy(cloud_seed) if cloud_seed is not None else None
        self.credentials, self.watcher = credentials, watcher
        self.result, self.error, self.cancelled = None, '', False

    def stop(self):
        self.requestInterruption()

    def runtime_context(self):
        if self.cloud_seed is None:
            return None
        cloud = self.cloud_seed
        try:
            cloud['connected'] = bool(self.credentials and self.credentials.connected())
        except Exception as error:
            cloud['connected'] = False
            cloud['connection_snapshot_error'] = type(error).__name__
        try:
            cloud['watcher'] = self.watcher.snapshot() if self.watcher is not None else None
        except Exception as error:
            cloud['watcher'] = None
            cloud['watcher_snapshot_error'] = type(error).__name__
        return {'cloud_watch':cloud}

    def run(self):
        try:
            if self.isInterruptionRequested():
                raise ExportCancelled()
            self.progress.emit('读取本次任务上下文')
            context = self.runtime_context()
            self.result = export_diagnostics(self.root,self.log_folder,self.destination,
                task_id=self.task_id,runtime_context=context,cutoff=self.cutoff,
                stop_requested=self.isInterruptionRequested,progress=self.progress.emit)
        except ExportCancelled:
            self.cancelled = True
        except Exception as error:
            self.error = type(error).__name__+'：'+redact(str(error))[:1000]
