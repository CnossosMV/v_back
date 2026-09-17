"""Injectable clock for deterministic policy and planning decisions."""

from datetime import datetime, timezone


class SystemClock:
    def utcnow(self) -> datetime:
        return datetime.utcnow()

    def now(self, tz=None) -> datetime:
        return datetime.now(tz) if tz else self.utcnow()


class FrozenClock(SystemClock):
    def __init__(self, value: datetime):
        self.value = value

    def utcnow(self) -> datetime:
        if self.value.tzinfo:
            return self.value.astimezone(timezone.utc).replace(tzinfo=None)
        return self.value

    def now(self, tz=None) -> datetime:
        value = self.value
        if tz:
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            return value.astimezone(tz)
        return self.utcnow()
