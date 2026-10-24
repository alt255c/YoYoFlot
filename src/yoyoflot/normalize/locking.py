"""Stable OS lock: process exit releases ownership; file existence is harmless.

Never unlink the file: another writer might already have opened that inode.
Empty lock files from 0.4.0 are compatible.
"""
import json
import os
import sys
from datetime import datetime, timezone


class OutputLock:
    def __init__(self, path, label="T11"):
        self.path = path
        self.label = label
        self.handle = None

    def __enter__(self):
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | getattr(os, "O_BINARY", 0), 0o600)
        handle = os.fdopen(fd, 'r+b', buffering=0)
        try:
            if os.fstat(handle.fileno()).st_size == 0:
                handle.write(b' ')
            handle.seek(0)
            if sys.platform == 'win32':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise RuntimeError(f'{self.label} уже выполняется другим процессом: {self.path.parent}') from exc
        try:
            payload = json.dumps(dict(pid=os.getpid(), started_at=datetime.now(timezone.utc).isoformat())).encode('utf-8')
            handle.seek(0)
            handle.write(payload)
            handle.truncate()
        except BaseException:
            handle.close()
            raise
        self.handle = handle
        return self

    def __exit__(self, *_):
        handle, self.handle = self.handle, None
        if handle is not None:
            handle.close()  # OS releases ownership, including on KeyboardInterrupt.
