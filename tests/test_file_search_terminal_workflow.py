"""Exercise a real editing workflow without a model or third-party runtime."""

import asyncio

from test_engine import python_command, request

from anywhere_computer.engine import Engine


async def test_search_read_edit_and_verify_in_persistent_terminal(tmp_path):
    engine = Engine(tmp_path / "state")

    async def call(tool, **arguments):
        result = await engine.execute(request(tool, **arguments))
        assert result.state == "completed", result
        return result.data

    try:
        work = tmp_path / "work"
        await call("directories_create", path=str(work))
        path = work / "日本語.txt"
        await call("files_write", path=str(path), text="値=40\n比較=40\n🚀\n")
        search = await call("search_start", path=str(work), pattern="40", kind="text")
        rows, cursor = [], 0
        async with asyncio.timeout(5):
            while True:
                page = await call(
                    "search_results", search_id=search["search_id"], cursor=cursor, limit=1,
                )
                rows.extend(page["results"])
                cursor = page["next_cursor"]
                if page["state"] == "completed" and cursor == page["total"]:
                    break
                await asyncio.sleep(0.01)
        assert [row["line"] for row in rows] == [1, 2]
        assert all(row["path"] == str(path.resolve()) for row in rows)
        batch = await call("files_read_many", paths=[str(path), str(work / "missing.txt")])
        read, missing = batch["files"]
        assert read["text"] == "値=40\n比較=40\n🚀\n"
        assert "error" in missing
        await call(
            "files_edit", path=read["path"], old_text="値=40", new_text="値=42",
            expected_sha256=read["sha256"],
        )
        script = (
            "import pathlib,sys\n"
            "sys.stdin.reconfigure(encoding='utf-8')\n"
            "sys.stdout.reconfigure(encoding='utf-8')\n"
            "for line in sys.stdin:\n"
            " print(pathlib.Path(line.strip()).read_text(encoding='utf-8'), end='')\n"
            " print('READY>', flush=True)\n"
        )
        terminal = await call("terminal_start", cwd=str(work), command=python_command(script))
        identity = terminal["session_id"]
        for expected in ("値=42", "値=50"):
            output = await call(
                "terminal_input", session_id=identity, text=str(path) + "\n",
                wait_ms=3000, wait_for_prompt="READY>",
            )
            if not output["prompt_matched"]:
                print(output["text"])
            assert output["prompt_matched"] and output["wait_reason"] == "prompt", output
            assert expected in output["text"] and "比較=40" in output["text"]
            assert "🚀" in output["text"] and output["dropped_bytes"] == 0
            if expected == "値=42":
                current = await call("files_read", path=str(path))
                await call(
                    "files_edit", path=str(path), old_text="値=42", new_text="値=50",
                    expected_sha256=current["sha256"],
                )
        stopped = await call("terminal_stop", session_id=identity)
        assert stopped["state"] == "exited"
    finally:
        await engine.close()


async def test_shared_file_handoff_rejects_stale_edit_and_survives_restart(tmp_path):
    """Two request sequences share real files; no browser/model is simulated here."""
    state = tmp_path / 'state'
    engine = Engine(state)
    work = tmp_path / 'shared 日本語'
    path = work / 'main.py'

    async def call(tool, **arguments):
        reply = await engine.execute(request(tool, **arguments))
        assert reply.state == 'completed', reply
        return reply

    try:
        await call('directories_create', path=str(work))
        created = await call('files_write', path=str(path), text='print(40)\n')
        reader_b = (await call('files_read', path=str(path))).data
        reader_a = (await call('files_read', path=str(path))).data
        assert reader_a['sha256'] == reader_b['sha256']
        await call('files_write', path=str(path), text='print(42)\n', mode='replace',
                   expected_sha256=reader_a['sha256'])
        rejected = await engine.execute(request(
            'files_write', path=str(path), text='print(99)\n', mode='replace',
            expected_sha256=reader_b['sha256']))
        assert rejected.state == 'failed'
        latest = (await call('files_read', path=str(path))).data
        assert latest['text'] == 'print(42)\n'
        assert latest['sha256'] != reader_b['sha256']
        terminal = (await call('terminal_start', cwd=str(work),
                              command=python_command(
                                  'import runpy; runpy.run_path("main.py")'))).data
        output = (await call('terminal_output', session_id=terminal['session_id'],
                            cursor=0, wait_ms=3000)).data
        async with asyncio.timeout(10):
            while output['state'] != 'exited':
                await asyncio.sleep(0.01)
                output = (await call('terminal_output', session_id=terminal['session_id'],
                                    cursor=0, wait_ms=1000)).data
        assert output['text'].strip() == '42'
        assert output['exit_code'] == 0
        await engine.close()
        engine = Engine(state)
        assert (await call('files_read', path=str(path))).data['text'] == 'print(42)\n'
        recovered = await call('operations_get', operation_id=created.operation_id)
        assert recovered.data['state'] == 'completed'
    finally:
        await engine.close()
