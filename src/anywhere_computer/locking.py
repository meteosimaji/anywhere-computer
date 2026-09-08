"""Kernel-released advisory locks through Python's platform standard library."""

import errno
import os
import sys
import time
from pathlib import Path
from types import TracebackType

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl


class ProcessLock:
    """Exclusive, non-reentrant lock. Keep the inode in place after release."""

    def __init__(self, path: Path, timeout: float = 0) -> None:
        self.path = path
        self.timeout = timeout
        self.fd: int | None = None

    def __enter__(self) -> "ProcessLock":
        if self.fd is not None:
            raise RuntimeError("Lock instance is already acquired")
        descriptor = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        deadline = time.monotonic() + self.timeout
        try:
            while True:
                try:
                    if sys.platform == "win32":
                        os.lseek(descriptor, 0, os.SEEK_SET)
                        msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                    else:
                        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    self.fd = descriptor
                    return self
                except OSError as error:
                    if error.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                        raise
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"Lock is busy: {self.path.name}") from None
                    time.sleep(min(0.05, max(0, deadline - time.monotonic())))
        except BaseException:
            os.close(descriptor)
            raise

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        descriptor, self.fd = self.fd, None
        if descriptor is not None:
            # Closing releases flock / Windows byte-range locks, also on process death.
            os.close(descriptor)
