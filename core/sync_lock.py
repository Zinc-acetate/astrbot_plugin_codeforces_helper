import fcntl
from contextlib import contextmanager
from pathlib import Path


class SyncAlreadyRunning(RuntimeError):
    pass


@contextmanager
def acquire_sync_lock(db_path):
    """跨主插件与 WebUI 子进程互斥，避免同时同步同一数据库。"""
    lock_path = Path(db_path).with_name("codeforces_helper.sync.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_file = lock_path.open("a+")
    try:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise SyncAlreadyRunning("已有数据更新任务正在运行") from exc
        yield
    finally:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        finally:
            lock_file.close()
