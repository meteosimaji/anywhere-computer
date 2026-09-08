"""OS process inspection and identity-checked termination using the existing psutil dependency."""

import os

import psutil
from pydantic import JsonValue

from .models import ListProcesses, StopProcess


def list_processes(args: ListProcesses) -> dict[str, JsonValue]:
    rows: list[JsonValue] = []
    unavailable = 0
    pids = sorted(pid for pid in psutil.pids() if pid > args.after_pid)
    scanned = pids[:args.limit]
    for pid in scanned:
        try:
            process = psutil.Process(pid)
            with process.oneshot():
                memory = process.memory_info()
                cpu = process.cpu_times()
                rows.append({"pid": pid, "name": process.name(), "created": process.create_time(),
                             "status": process.status(), "rss_bytes": memory.rss,
                             "cpu_user_seconds": cpu.user, "cpu_system_seconds": cpu.system})
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            unavailable += 1
    return {"processes": rows, "unavailable": unavailable, "truncated": len(pids) > len(scanned),
            "next_pid": scanned[-1] if scanned else args.after_pid}


def stop_process(args: StopProcess) -> dict[str, JsonValue]:
    current = psutil.Process(os.getpid())
    protected = {current.pid, *(parent.pid for parent in current.parents())}
    if args.pid in protected:
        raise ValueError("Cannot terminate the agent or its ancestors through this tool")
    process = psutil.Process(args.pid)
    if process.create_time() != args.created:
        raise ValueError("Process identity changed; inspect the process list again")
    if args.force:
        process.kill()
    else:
        process.terminate()
    try:
        code = process.wait(timeout=3)
        return {"pid": args.pid, "state": "exited", "exit_code": code}
    except psutil.TimeoutExpired:
        return {"pid": args.pid, "state": "termination_requested", "exit_code": None}
