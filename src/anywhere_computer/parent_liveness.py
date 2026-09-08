"""A private stdin pipe reports owner loss without PID guessing or polling."""

import os
from threading import Event, Thread


def watch_parent_pipe() -> Event:
    """Return an event set on EOF, unexpected data, or an unreadable owner pipe.

    Only internal child launches opt in. The owner keeps the sole write handle
    open and sends no data. OS handle closure on owner death produces EOF even
    when Python cleanup never runs. The daemon reader lives only for this CLI.
    """
    stopped = Event()

    def observe() -> None:
        try:
            os.read(0, 1)
        except (OSError, ValueError):
            pass
        finally:
            stopped.set()

    Thread(target=observe, name="anywhere-parent-pipe", daemon=True).start()
    return stopped
