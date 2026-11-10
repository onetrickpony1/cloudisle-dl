import time, shutil, os
from pathlib import Path

RESERVE = 50 * 1024**3


class SpaceGuard:
    def __init__(
        self,
        dest,
        ttl=2,
        margin=256 * 1024**2,
        usage=shutil.disk_usage,
        mounted=os.path.ismount,
        clock=time.monotonic,
        reserve=RESERVE,
    ):
        self.dest = Path(dest)
        self.ttl = ttl
        self.margin = margin
        self.usage = usage
        self.mounted = mounted
        self.clock = clock
        self.free = None
        self.checked = 0
        self.queries = 0
        self.reserve = reserve

    def check(self, need=0, consume=False):
        if need < 0:
            raise ValueError("Negative space requirement")
        if not self.mounted(self.dest.parent):
            self.free = None
            raise RuntimeError("Destination share is not mounted")
        now = self.clock()
        if (
            self.free is None
            or now - self.checked >= self.ttl
            or need >= 8 * 1024**2
            or self.free - need < self.reserve + self.margin
        ):
            self.free = None
            self.free = self.usage(self.dest).free
            self.checked = now
            self.queries += 1
        if self.free < self.reserve + need:
            raise RuntimeError("Insufficient share capacity; reserve retained")
        if consume:
            self.free -= need
