"""跨进程文件锁（Windows 兼容，无第三方依赖）。

用法：
  from .file_lock import file_lock
  with file_lock(path, timeout_s=30, stale_after_s=120):
      ... 原子写 ...

语义：
- lock 文件用 O_CREAT|O_EXCL 原子创建，杜绝竞态；
- 持锁进程崩溃留下的死锁文件超过 stale_after_s 自动抢占；
- 超时抛 TimeoutError（调用方决定告警/重试），绝不静默等待。
"""
import os
import time
from pathlib import Path


class LockHandle:
    def __init__(self, lock_path: Path):
        self.path = lock_path

    def release(self):
        try:
            self.path.unlink()
        except OSError:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.release()
        return False


def file_lock(target: Path, timeout_s: float = 30.0, stale_after_s: float = 120.0) -> LockHandle:
    """获取 target 对应的 .lock 文件锁。失败抛 TimeoutError。"""
    lock = target.with_name(target.name + ".lock")
    start = time.time()
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(fd, f"{os.getpid()} {time.time()}".encode("utf-8"))
            finally:
                os.close(fd)
            return LockHandle(lock)
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime > stale_after_s:
                    lock.unlink()  # 死锁文件过期 → 抢占
                    continue
            except OSError:
                pass
            if time.time() - start > timeout_s:
                raise TimeoutError(
                    f"无法获取文件锁 {lock}（已等待 {timeout_s}s，可能被并发 council/ingest 占用）")
            time.sleep(0.2)
