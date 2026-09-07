import errno
import os
from contextlib import contextmanager
from pathlib import Path

if os.name == "nt":
    import msvcrt
else:
    import fcntl


class SyncAlreadyRunning(RuntimeError):
    pass


@contextmanager
def acquire_sync_lock(db_path):
    """跨主插件与 WebUI 子进程互斥，避免同时同步同一数据库。"""
    lock_path = Path(db_path).with_name("codeforces_helper.sync.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_file = lock_path.open("a+b")
    acquired = False
    try:
        try:
            if os.name == "nt":
                # Windows byte-range locks also work beyond EOF. Always use byte 0.
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except OSError as exc:
            if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                raise
            raise SyncAlreadyRunning("已有数据更新任务正在运行") from exc
        yield
    finally:
        try:
            if acquired:
                if os.name == "nt":
                    lock_file.seek(0)
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        finally:
            lock_file.close()
