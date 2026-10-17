"""Production-only scheduling and safe-boundary control; no Qt or publisher state."""
from datetime import datetime, timedelta, timezone
import math
import time

BEIJING = timezone(timedelta(hours=8))


class SafePause(Exception):
    """An intentional checkpoint, never a synthesis failure."""


class ProductionControl:
    def __init__(self, cfg, monotonic=time.monotonic, now=None):
        self.cfg = cfg
        self.clock = monotonic
        self.now = now or (lambda: datetime.now(BEIJING))
        self.started = self.clock()
        self.cool_started = None
        self.prior_cooling_seconds = 0.0
        self.excluded = 0.0
        self.reasons = set()
        self.thermal_pending = False
        self.deadline = None
        self.budget = None
        if cfg.get('execution_duration_enabled', False):
            minutes = float(cfg.get('execution_duration_minutes', 60))
            if not math.isfinite(minutes) or minutes <= 0:
                raise ValueError('执行时长必须为正数')
            self.budget = minutes * 60
        if cfg.get('execution_time_enabled', False):
            hour, minute = map(int, str(cfg.get('execution_time', '23:00')).split(':'))
            current = self.now().astimezone(BEIJING)
            target = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
            self.deadline = target if target > current else target + timedelta(days=1)
        if cfg.get('gpu_protect_enabled'):
            if not (float(cfg.get('gpu_temp_resume', 60)) < float(cfg.get('gpu_temp_limit', 75))):
                raise ValueError('GPU复工温度必须低于上限')
            if float(cfg.get('gpu_cooling_minutes', 1)) <= 0:
                raise ValueError('GPU冷却时长必须为正数')

    def poll(self):
        end = self.cool_started if self.cool_started is not None else self.clock()
        if self.budget is not None and end - self.started - self.excluded >= self.budget:
            self.reasons.add('执行时长到达')
        if self.deadline is not None and self.now().astimezone(BEIJING) >= self.deadline:
            self.reasons.add('北京时间点到达')
        return bool(self.reasons)

    def observe_temperature(self, temperature):
        if self.cfg.get('gpu_protect_enabled') and temperature is not None:
            if temperature >= float(self.cfg.get('gpu_temp_limit', 75)):
                self.thermal_pending = True

    def begin_cooling(self):
        if self.cool_started is None:
            self.cool_started = self.clock()

    def cooled(self, temperature):
        return (self.cool_started is not None and temperature is not None
                and self.clock() - self.cool_started + self.prior_cooling_seconds >= float(self.cfg.get('gpu_cooling_minutes', 1)) * 60
                and temperature <= float(self.cfg.get('gpu_temp_resume', 60)))

    def end_cooling(self):
        if self.cool_started is not None:
            self.excluded += self.clock() - self.cool_started
        self.cool_started = None
        self.prior_cooling_seconds = 0.0
        self.thermal_pending = False
