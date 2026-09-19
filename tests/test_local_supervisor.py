from threading import Event

from anywhere_computer import local_supervisor
from anywhere_computer.watch_status import read_watch_observation


def test_local_supervisor_reuses_selection_and_leaves_engine_running(tmp_path, monkeypatch):
    stop = Event()
    calls = []

    def ensure(directory, **kwargs):
        calls.append((directory, kwargs))
        stop.set()
        return {"state": "ready"}

    monkeypatch.setattr(local_supervisor, "ensure_agent", ensure)
    assert local_supervisor.watch_local(tmp_path, stop=stop) == 130
    assert calls == [(tmp_path, {})]  # No replace_idle or stop request.
    observed = read_watch_observation(tmp_path / "local-watch-status.json")
    assert observed["last_observation"]["event"] == "interrupted"


def test_local_supervisor_blocks_error_without_reprompting(tmp_path, monkeypatch):
    stop = Event()
    calls = []
    observations = []

    def rejected(directory):
        calls.append(directory)
        raise RuntimeError("private credential diagnostic")

    def saved(path, event, *args):
        observations.append(event)
        if event == "blocked":
            stop.set()

    monkeypatch.setattr(local_supervisor, "ensure_agent", rejected)
    monkeypatch.setattr(local_supervisor, "save_watch_observation", saved)
    assert local_supervisor.watch_local(tmp_path, stop=stop) == 130
    assert calls == [tmp_path]
    assert observations == ["blocked", "interrupted"]


async def test_local_watch_does_not_replace_or_stop_live_engine(tmp_path, monkeypatch):
    import asyncio
    import time

    from anywhere_computer import connection
    from anywhere_computer.connection import exchange, serve

    # Keep the original startup deadline. Record real stages so a Windows
    # timeout identifies unfinished work instead of inviting blind retries.

    stages = {}
    original_engine = connection.Engine
    original_start_server = asyncio.start_server

    def measured_engine(*args, **kwargs):
        started = time.monotonic()
        try:
            return original_engine(*args, **kwargs)
        finally:
            stages["engine_init_seconds"] = time.monotonic() - started

    async def measured_server(*args, **kwargs):
        started = time.monotonic()
        try:
            return await original_start_server(*args, **kwargs)
        finally:
            stages["server_open_seconds"] = time.monotonic() - started

    monkeypatch.setattr(connection, "Engine", measured_engine)
    monkeypatch.setattr(asyncio, "start_server", measured_server)

    monkeypatch.setattr("anywhere_computer.connection.local_credential", lambda *a, **k: "fixture")
    # This fixture runs the watcher in a worker thread; OS signal handling is
    # tested by CLI/native tests, not Python's main-thread-only signal API.
    monkeypatch.setattr(local_supervisor.signal, "signal", lambda *a: None)
    engine_stop = asyncio.Event()
    watch_stop = Event()
    engine = asyncio.create_task(serve(tmp_path, credential="fixture", shutdown=engine_stop))
    watcher = None
    try:
        try:
            async with asyncio.timeout(5):
                while not (tmp_path / "agent.json").exists():
                    if engine.done():
                        await engine
                        raise AssertionError("Engine exited before publishing its endpoint")
                    await asyncio.sleep(0.01)
        except TimeoutError as error:
            frames = [frame.f_code.co_name for frame in engine.get_stack()]
            raise AssertionError(
                f"Engine startup exceeded 5 seconds: stages={stages}, "
                f"done={engine.done()}, stack={frames}"
            ) from error
        before = await exchange(tmp_path, "__status", credential="fixture")
        watcher = asyncio.create_task(asyncio.to_thread(
            local_supervisor.watch_local, tmp_path, stop=watch_stop, interval=1,
        ))
        async with asyncio.timeout(5):
            while not (tmp_path / "local-watch-status.json").exists():
                await asyncio.sleep(0.01)
        observation = read_watch_observation(tmp_path / "local-watch-status.json")
        assert observation["last_observation"]["event"] == "engine_ready"
        watch_stop.set()
        assert await watcher == 130
        after = await exchange(tmp_path, "__status", credential="fixture")
        assert before.data["instance_id"] == after.data["instance_id"]
        assert not engine.done()
    finally:
        watch_stop.set()
        if watcher is not None:
            await watcher
        engine_stop.set()
        await engine
