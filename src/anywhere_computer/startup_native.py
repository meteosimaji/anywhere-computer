"""Small user-session adapters; commands never interpolate paths into shell code."""

import hashlib
import json
import os
import plistlib
import re
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from .autostart import TASK_NAMESPACE, StartupDefinition


@dataclass(frozen=True)
class StartupSnapshot:
    present: bool
    matches: bool = False
    running: bool = False
    enabled: bool = False
    fingerprint: str = ""
    raw: str = ""


def _fingerprint(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _gui_domain() -> str:
    get_uid = getattr(os, "getuid", None)
    if get_uid is None:
        raise RuntimeError("A macOS user session is required")
    return f"gui/{get_uid()}"


def _run(command: list[str], *, payload: str | None = None) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(command, input=payload, capture_output=True,
                                encoding="utf-8", timeout=30)
    except (OSError, subprocess.TimeoutExpired, UnicodeError):
        raise RuntimeError("OS startup manager did not return a readable response") from None
    if len(result.stdout) + len(result.stderr) > 262144:
        raise RuntimeError("OS startup manager response exceeds the limit")
    return result


def _checked(command: list[str], *, payload: str | None = None) -> str:
    result = _run(command, payload=payload)
    if result.returncode:
        raise RuntimeError(f"OS startup manager command failed (exit {result.returncode})")
    return result.stdout


_WINDOWS_SCRIPT = r'''
$ErrorActionPreference="Stop"
[Console]::InputEncoding=New-Object System.Text.UTF8Encoding
[Console]::OutputEncoding=New-Object System.Text.UTF8Encoding
$request=[Console]::In.ReadToEnd() | ConvertFrom-Json
$identity=[Security.Principal.WindowsIdentity]::GetCurrent()
$scheduler=New-Object -ComObject "Schedule.Service"
$scheduler.Connect()
$folder=$scheduler.GetFolder("\")
$task=$null
foreach ($candidate in $folder.GetTasks(1)) {
    if ($candidate.Name -ieq $request.name) { $task=$candidate; break }
}
if ($request.operation -eq "install") {
    if ($null -ne $task) { throw "Task already exists" }
    $definition=$scheduler.NewTask(0)
    $definition.XmlText=$request.xml
    $definition.Principal.UserId=$identity.User.Value
    $definition.Triggers.Item(1).UserId=$identity.User.Value
    $task=$folder.RegisterTask($request.name,$definition.XmlText,2,$null,$null,3,$null)
    $null=$task.Run($null)
} elseif ($request.operation -eq "uninstall") {
    if ($null -ne $task) {
        if ($task.Xml -cne $request.expectedXml) { throw "Task changed" }
        $task.Stop(0)
        for ($attempt=0; $attempt -lt 50 -and $task.GetInstances(0).Count -ne 0; $attempt++) {
            Start-Sleep -Milliseconds 100
        }
        if ($task.GetInstances(0).Count -ne 0) { throw "Task is still running" }
        $folder.DeleteTask($request.name,0)
        $task=$null
    }
} elseif ($request.operation -eq "start") {
    if ($null -eq $task) { throw "Task is absent" }
    if ($task.Xml -cne $request.expectedXml) { throw "Task changed" }
    $null=$task.Run($null)
} elseif ($request.operation -ne "query") { throw "Unknown operation" }
$answer=@{present=($null -ne $task); sid=$identity.User.Value; user=$identity.Name}
if ($null -ne $task) {
    $answer.xml=$task.Xml
    $answer.running=($task.State -eq 4)
    $answer.enabled=$task.Enabled
}
[Console]::Out.Write(($answer | ConvertTo-Json -Depth 5 -Compress))
'''


class NativeStartup:
    def __init__(self, definition: StartupDefinition) -> None:
        self.definition = definition

    def _windows(self, operation: str, *, expected_xml: str = "") -> dict[str, object]:
        executable = (Path(os.environ.get("SystemRoot", r"C:\Windows")) /
                      "System32/WindowsPowerShell/v1.0/powershell.exe")
        payload = json.dumps({
            "name": self.definition.name, "operation": operation,
            "xml": self.definition.content.decode("utf-16"), "expectedXml": expected_xml,
        })
        raw = _checked([str(executable), "-NoProfile", "-NonInteractive", "-Command",
                        _WINDOWS_SCRIPT], payload=payload)
        value = json.loads(raw)
        if not isinstance(value, dict) or type(value.get("present")) is not bool:
            raise RuntimeError("Invalid Windows startup manager response")
        return value

    def _systemd(self, *arguments: str) -> str:
        return _checked(["/usr/bin/systemctl", "--user", *arguments])

    def _mac_target(self) -> str:
        return f"{_gui_domain()}/{self.definition.name}"

    def _mac_enabled(self) -> bool:
        output = _checked(["/bin/launchctl", "print-disabled", _gui_domain()])
        quoted = '"' + self.definition.name + '"'
        pattern = re.compile(r"^[ \t]*" + re.escape(quoted) +
                             r"[ \t]*=>[ \t]*(enabled|disabled)[ \t]*$")
        for line in output.splitlines():
            match = pattern.fullmatch(line)
            if match:
                return match.group(1) == "enabled"
            if quoted in line:
                raise RuntimeError("Could not inspect the user LaunchAgent enabled state")
        # An absent entry has no persistent enable/disable override. The plist's
        # optional Disabled key is absent from our generated definitions.
        return True

    def query(self) -> StartupSnapshot:
        definition = self.definition
        if definition.platform == "darwin":
            result = _run(["/bin/launchctl", "print", self._mac_target()])
            if result.returncode == 113:
                # Distinguish an absent job from an unavailable user GUI domain.
                _checked(["/bin/launchctl", "print", _gui_domain()])
                return StartupSnapshot(False)
            if result.returncode:
                raise RuntimeError("Could not inspect the user LaunchAgent")
            expected = plistlib.loads(definition.content)
            fields = {}
            for key in ("path", "program", "working directory", "state"):
                match = re.search(r"^\t" + key + r" = (.*)$", result.stdout, re.MULTILINE)
                fields[key] = match.group(1) if match else ""
            block = re.search(r"^\targuments = \{\n(.*?)^\t\}", result.stdout,
                              re.MULTILINE | re.DOTALL)
            arguments = [] if block is None else [
                line[2:] for line in block.group(1).splitlines() if line.startswith("\t\t")
            ]
            mac_identity = [fields["path"], fields["program"], fields["working directory"],
                            arguments]
            wanted = [str(definition.path), expected["ProgramArguments"][0],
                      expected["WorkingDirectory"], expected["ProgramArguments"]]
            return StartupSnapshot(True, mac_identity == wanted, fields["state"] == "running",
                                   self._mac_enabled(), _fingerprint(mac_identity))
        if definition.platform == "linux":
            output = self._systemd(
                "show", definition.name + ".service", "--property=LoadState,FragmentPath,"
                "DropInPaths,Transient,NeedDaemonReload,ActiveState,SubState,UnitFileState",
            )
            properties = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
            if properties.get("LoadState") == "not-found" and not properties.get("FragmentPath"):
                if (properties.get("UnitFileState", "") not in {"", "not-found"}
                        or properties.get("ActiveState") != "inactive"
                        or properties.get("SubState") != "dead"
                        or properties.get("DropInPaths", "")
                        or properties.get("Transient") != "no"):
                    return StartupSnapshot(True)
                return StartupSnapshot(False)
            linux_identity = {key: properties.get(key, "") for key in
                              ("FragmentPath", "DropInPaths", "Transient", "NeedDaemonReload")}
            expected_identity = {"FragmentPath": str(definition.path), "DropInPaths": "",
                                 "Transient": "no", "NeedDaemonReload": "no"}
            return StartupSnapshot(
                True, linux_identity == expected_identity,
                properties.get("ActiveState") == "active",
                properties.get("UnitFileState") == "enabled", _fingerprint(linux_identity),
            )
        value = self._windows("query")
        if not value["present"]:
            return StartupSnapshot(False)
        raw, sid, user = value.get("xml"), value.get("sid"), value.get("user")
        if not all(isinstance(item, str) for item in (raw, sid, user)):
            raise RuntimeError("Invalid Windows task identity")
        assert isinstance(raw, str) and isinstance(sid, str) and isinstance(user, str)
        actual, expected = ET.fromstring(raw), ET.fromstring(definition.content)
        ns = {"t": TASK_NAMESPACE}

        def field(root: ET.Element, path: str) -> str:
            # Task Scheduler omits this default from its stored XML (MS-TSCH).
            default = "LeastPrivilege" if path == "t:Principals/t:Principal/t:RunLevel" else ""
            return root.findtext(path, default=default, namespaces=ns)

        paths = ("t:Actions/t:Exec/t:Command", "t:Actions/t:Exec/t:Arguments",
                 "t:Actions/t:Exec/t:WorkingDirectory", "t:Principals/t:Principal/t:LogonType",
                 "t:Principals/t:Principal/t:RunLevel", "t:RegistrationInfo/t:Description")
        matches = all(field(actual, path) == field(expected, path) for path in paths)
        for path in ("t:Principals/t:Principal/t:UserId", "t:Triggers/t:LogonTrigger/t:UserId"):
            matches &= field(actual, path).casefold() in {sid.casefold(), user.casefold()}
            matches &= field(expected, path).casefold() in {sid.casefold(), user.casefold()}
        matches &= len(actual.findall("t:Actions/*", ns)) == 1
        matches &= len(actual.findall("t:Triggers/*", ns)) == 1
        return StartupSnapshot(True, matches, value.get("running") is True,
                               value.get("enabled") is True,
                               _fingerprint(ET.canonicalize(raw)), raw)

    def install(self) -> None:
        definition = self.definition
        if definition.platform == "darwin":
            _checked(["/bin/launchctl", "bootstrap", _gui_domain(), str(definition.path)])
        elif definition.platform == "linux":
            self._systemd("daemon-reload")
            if not self.query().matches:
                raise RuntimeError("Loaded systemd unit does not match the startup definition")
            self._systemd("enable", "--now", definition.name + ".service")
        else:
            self._windows("install")

    def start(self, snapshot: StartupSnapshot) -> None:
        current = self.query()
        if (not current.present or not current.matches or
                current.fingerprint != snapshot.fingerprint):
            raise RuntimeError("OS startup definition changed before start")
        if self.definition.platform == "darwin":
            _checked(["/bin/launchctl", "kickstart", self._mac_target()])
        elif self.definition.platform == "linux":
            self._systemd("start", self.definition.name + ".service")
        else:
            self._windows("start", expected_xml=current.raw)

    def uninstall(self, snapshot: StartupSnapshot) -> None:
        current = self.query()
        if not current.present:
            return
        if not current.matches or current.fingerprint != snapshot.fingerprint:
            raise RuntimeError("OS startup definition changed before removal")
        if self.definition.platform == "darwin":
            _checked(["/bin/launchctl", "bootout", self._mac_target()])
        elif self.definition.platform == "linux":
            self._systemd("disable", "--now", self.definition.name + ".service")
        else:
            self._windows("uninstall", expected_xml=current.raw)

    def files_removed(self) -> None:
        if self.definition.platform == "linux":
            self._systemd("daemon-reload")
