"""One continuously updated, atomic report per deploy start/stop session."""
import json
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path

DEFAULT_REPORT_DIR = Path(__file__).resolve().parents[3] / "data" / "publish_reports"
CHINA_TIME = timezone(timedelta(hours=8))


class PublishSessionReport:
    def __init__(self, output_dir: Path):
        output_dir.mkdir(parents=True, exist_ok=True)
        self.path = output_dir / f"publish_task_{datetime.now(CHINA_TIME).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}.json"
        self.data = {"task_id": self.path.stem, "started_at": self._now(),
                     "finished_at": None, "status": "RUNNING", "success_count": 0,
                     "failure_count": 0, "cancelled_count": 0, "unconfirmed_count": 0,
                     "attempt_count": 0, "files": [], "blocked_accounts": []}
        self._save()

    @staticmethod
    def _now():
        return datetime.now(CHINA_TIME).isoformat(timespec="seconds")

    def begin_file(self, account, file_path: Path):
        stat = file_path.stat()
        resolved = str(file_path.resolve())
        item = next((x for x in self.data['files'] if x['account_id'] == account.account_id
                     and x['file_path'] == resolved), None)
        if item is None:
            item = {"file_name": file_path.name, "file_path": resolved,
                "size_bytes": stat.st_size, "modified_at": datetime.fromtimestamp(stat.st_mtime, CHINA_TIME).isoformat(timespec="seconds"),
                "account_id": account.account_id, "account_name": account.account_name,
                "started_at": self._now(), "attempts": []}
            self.data["files"].append(item)
        item.update(status='IN_PROGRESS', finished_at=None)
        item['attempts'].append({'attempt_number': len(item['attempts']) + 1,
                                 'started_at': self._now(), 'status': 'IN_PROGRESS', 'logs': []})
        self._save()
        return item

    def complete_file(self, item, status, message, record=None):
        item.update(status=status, message=message, finished_at=self._now())
        item['attempts'][-1].update(status=status, message=message, finished_at=item['finished_at'],
                                    last_stage=item.get('last_stage', ''))
        if record:
            item.update(publish_info=record.to_dict())
        self._save()

    def log_progress(self, item, message):
        item['last_stage'] = message
        logs = item['attempts'][-1]['logs']
        if len(logs) >= 100:
            logs.pop(0)
        logs.append({'time': self._now(), 'message': message})
        self._save()

    def finish(self, status):
        self.data.update(status=status, finished_at=self._now())
        self._save()

    def _save(self):
        for status, field in (("SUCCESS", "success_count"), ("FAILED", "failure_count"),
                              ("CANCELLED", "cancelled_count"), ("UNCONFIRMED", "unconfirmed_count")):
            statuses = {status, 'ACCOUNT_LIMIT', 'DURATION_LIMIT'} if status == 'FAILED' else {status}
            self.data[field] = sum(item["status"] in statuses for item in self.data["files"])
            self.data[field.replace("_count", "_files")] = [item for item in self.data["files"] if item["status"] in statuses]
        self.data["attempt_count"] = sum(len(item['attempts']) for item in self.data['files'])
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)
