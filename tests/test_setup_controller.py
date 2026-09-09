import asyncio

import pytest

from anywhere_computer import setup_controller
from anywhere_computer.remote_setup import plan_remote_setup
from anywhere_computer.setup_controller import SetupController


async def test_confirmation_reconciles_lost_response_and_repeated_clicks(tmp_path, monkeypatch):
    controller = SetupController(tmp_path / "setup")
    assert controller.progress().phase == "new"
    assert not controller.directory.exists()
    plan = await plan_remote_setup(resource="https://fixture.example/mcp")
    review = controller.review(plan)
    assert review.phase == "review"
    real_save = setup_controller.save_http_config
    calls = 0

    async def lost_response(directory, config):
        nonlocal calls
        calls += 1
        await real_save(directory, config)
        raise OSError("synthetic lost result")

    monkeypatch.setattr(setup_controller, "save_http_config", lost_response)
    results = await asyncio.gather(controller.confirm(review.plan_id),
                                   controller.confirm(review.plan_id))
    assert all(result.phase == "configured" for result in results)
    assert calls == 1
    assert SetupController(controller.directory).progress().configuration == plan


async def test_stale_plan_and_existing_configuration_never_overwrite(tmp_path):
    controller = SetupController(tmp_path)
    first = controller.review(await plan_remote_setup(resource="https://first.example/mcp"))
    second = controller.review(await plan_remote_setup(resource="https://second.example/mcp"))
    with pytest.raises(ValueError, match="no longer current"):
        await controller.confirm(first.plan_id)
    assert (await controller.confirm(second.plan_id)).phase == "configured"
    before = (tmp_path / "http-server/config.json").read_bytes()
    other = controller.review(await plan_remote_setup(resource="https://third.example/mcp"))
    assert (await controller.confirm(other.plan_id)).phase == "conflict"
    assert (tmp_path / "http-server/config.json").read_bytes() == before


async def test_pending_save_cannot_be_replaced_and_unpublished_failure_is_retryable(
    tmp_path, monkeypatch,
):
    controller = SetupController(tmp_path)
    plan = await plan_remote_setup(resource="https://fixture.example/mcp")
    review = controller.review(plan)
    started, release = asyncio.Event(), asyncio.Event()

    async def failed_save(directory, config):
        started.set()
        await release.wait()
        raise OSError("synthetic unpublished failure")

    monkeypatch.setattr(setup_controller, "save_http_config", failed_save)
    pending = asyncio.create_task(controller.confirm(review.plan_id))
    await started.wait()
    assert controller.progress().phase == "saving"
    with pytest.raises(RuntimeError, match="pending"):
        controller.review(plan)
    release.set()
    result = await pending
    assert result.phase == "review" and result.error
    assert not (tmp_path / "http-server").exists()


async def test_corrupt_saved_state_is_not_replaced(tmp_path):
    directory = tmp_path / "http-server"
    directory.mkdir()
    (directory / "config.json").write_text("not valid configuration")
    controller = SetupController(tmp_path)
    view = controller.review(await plan_remote_setup(resource="https://fixture.example/mcp"))
    assert view.phase == "invalid"
    with pytest.raises(ValueError, match="no longer current"):
        await controller.confirm("no-valid-plan")
    assert (directory / "config.json").read_text() == "not valid configuration"
