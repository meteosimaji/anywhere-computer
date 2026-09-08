"""User-session startup definitions for the shared remote supervisor."""

import hashlib
import os
import plistlib
import subprocess
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

Platform = Literal["darwin", "linux", "win32"]
TASK_NAMESPACE = "http://schemas.microsoft.com/windows/2004/02/mit/task"


@dataclass(frozen=True)
class StartupDefinition:
    platform: Platform
    name: str
    path: Path
    content: bytes


def _clean_argument(value: str) -> str:
    if not value or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("Startup arguments must be nonempty and contain no control characters")
    return value


def _systemd_quote(value: str) -> str:
    value = _clean_argument(value).replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    return '"' + value + '"'


def startup_definition(
    directory: Path,
    *,
    platform: Platform,
    home: Path,
    executable: str,
    user: str,
) -> StartupDefinition:
    directory = directory.resolve()
    executable = _clean_argument(executable)
    if not Path(executable).is_absolute():
        raise ValueError("Startup requires an absolute interpreter path")
    _clean_argument(str(directory))
    _clean_argument(user)
    name = "io.anywhere-computer." + hashlib.sha256(str(directory).encode()).hexdigest()[:20]
    arguments = ["-m", "anywhere_computer.cli", "remote-watch", "--state-dir", str(directory)]
    if platform == "darwin":
        content = plistlib.dumps({
            "Label": name,
            "ProgramArguments": [executable, *arguments],
            "WorkingDirectory": str(directory),
            "RunAtLoad": True,
            "KeepAlive": False,
            "ProcessType": "Background",
            "Umask": 0o077,
        }, sort_keys=True)
        return StartupDefinition(platform, name, home / "Library/LaunchAgents" / (name + ".plist"),
                                 content)
    if platform == "linux":
        # ':' disables environment expansion, including literal '$' in paths.
        command = ":" + " ".join(_systemd_quote(value) for value in [executable, *arguments])
        content = (
            "[Unit]\nDescription=Anywhere Computer remote connection\n"
            "After=graphical-session.target\n\n[Service]\nType=simple\n"
            f"ExecStart={command}\nWorkingDirectory={_systemd_quote(str(directory))}\n"
            "Restart=no\nUMask=0077\nKillMode=control-group\nTimeoutStopSec=30\n"
            "\n[Install]\nWantedBy=default.target\n"
        ).encode()
        return StartupDefinition(
            platform, name, home / ".config/systemd/user" / (name + ".service"), content
        )
    if platform != "win32":
        raise ValueError("Unsupported startup platform")
    for path in (executable, str(directory)):
        if len(path.encode("utf-16-le")) // 2 > 260:
            raise ValueError("Windows startup paths must be at most 260 UTF-16 code units")
        if "%" in path:
            raise ValueError("Windows startup paths cannot contain '%' (scheduler expansion)")

    def add(parent: ET.Element, tag: str, value: str | None = None) -> ET.Element:
        element = ET.SubElement(parent, tag)
        element.text = value
        return element

    root = ET.Element("Task", {"xmlns": TASK_NAMESPACE, "version": "1.3"})
    registration = add(root, "RegistrationInfo")
    add(registration, "Description", "Anywhere Computer: " + name)
    trigger = add(add(root, "Triggers"), "LogonTrigger")
    add(trigger, "Enabled", "true")
    add(trigger, "UserId", user)
    principal = add(add(root, "Principals"), "Principal")
    principal.set("id", "Owner")
    add(principal, "UserId", user)
    add(principal, "LogonType", "InteractiveToken")
    add(principal, "RunLevel", "LeastPrivilege")
    settings = add(root, "Settings")
    for key, value in {
        "MultipleInstancesPolicy": "IgnoreNew", "DisallowStartIfOnBatteries": "false",
        "StopIfGoingOnBatteries": "false", "AllowStartOnDemand": "true", "Enabled": "true",
        "ExecutionTimeLimit": "PT0S",
    }.items():
        add(settings, key, value)
    actions = add(root, "Actions")
    actions.set("Context", "Owner")
    action = add(actions, "Exec")
    add(action, "Command", executable)
    add(action, "Arguments", subprocess.list2cmdline(arguments))
    add(action, "WorkingDirectory", str(directory))
    content = ET.tostring(root, encoding="utf-16", xml_declaration=True)
    return StartupDefinition(platform, name, directory / "autostart" / (name + ".xml"), content)


def current_definition(directory: Path) -> StartupDefinition:
    import psutil

    if sys.platform not in {"darwin", "linux", "win32"}:
        raise ValueError("Unsupported startup platform")
    # Keep the venv path: resolving an interpreter symlink would lose its environment.
    definition = startup_definition(
        directory, platform=cast(Platform, sys.platform), home=Path.home(),
        executable=os.path.abspath(sys.executable), user=psutil.Process().username(),
    )
    if sys.platform == "linux":
        configured = os.environ.get("XDG_CONFIG_HOME", "")
        if configured:
            if not Path(configured).is_absolute():
                raise ValueError("XDG_CONFIG_HOME must be absolute for startup registration")
            definition = StartupDefinition(
                definition.platform, definition.name,
                Path(configured) / "systemd/user" / (definition.name + ".service"),
                definition.content,
            )
    return definition


def preview_startup(directory: Path) -> dict[str, str | bool]:
    """Render a definition without creating state or contacting a service manager."""
    definition = current_definition(directory)
    return {
        "platform": definition.platform,
        "name": definition.name,
        "definition_path": str(definition.path),
        "definition": definition.content.decode(
            "utf-16" if definition.platform == "win32" else "utf-8"
        ),
        "definition_sha256": hashlib.sha256(definition.content).hexdigest(),
        "registration_state": "unverified",
        "changed": False,
        "readiness_checked": False,
    }
