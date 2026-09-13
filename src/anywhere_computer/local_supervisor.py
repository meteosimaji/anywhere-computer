"""Observe the shared local engine without owning or replacing its work."""

import signal
from pathlib import Path
from threading import Event

from keyring.errors import KeyringError

from .connection import ensure_agent
from .locking import ProcessLock
from .state import prepare_directory
from .watch_status import WatchEvent, save_watch_observation


def watch_local(directory: Path, *, stop: Event | None = None, interval: float = 15) -> int:
    """Login supervision reuses normal runtime selection; uninstall stops this watcher.

    An engine explicitly stopped while supervision is enabled is started again.
    A live but unresponsive engine is never killed. Credential/configuration
    failures block until this watcher is restarted, avoiding repeated prompts.
    Stopping the watcher leaves the shared engine and its active sessions alive.
    """
    if interval < 1:
        raise ValueError("Local supervision interval must be at least one second")
    prepare_directory(directory)
    stopped = stop if stop is not None else Event()
    with ProcessLock(directory / "local-watch.lock"):
        previous = signal.signal(signal.SIGTERM, lambda *_: stopped.set())
        path = directory / "local-watch-status.json"

        def observe(event: WatchEvent) -> None:
            try:
                save_watch_observation(path, event, 0, 0, None)
            except (OSError, ValueError):
                pass

        try:
            while not stopped.is_set():
                try:
                    ensure_agent(directory)
                except (OSError, RuntimeError, ValueError, KeyringError):
                    observe("blocked")
                    stopped.wait()
                    break
                observe("engine_ready")
                stopped.wait(interval)
            observe("interrupted")
            return 130
        except KeyboardInterrupt:
            observe("interrupted")
            return 130
        finally:
            signal.signal(signal.SIGTERM, previous)
