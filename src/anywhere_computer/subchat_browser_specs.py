"""Installed Chromium products that may own a dedicated Subchat profile."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BrowserSpec:
    identifier: str
    display_name: str
    platforms: frozenset[str]
    playwright_channel: str
    windows_executable: str


# Only products with a verified dedicated-profile workflow belong here. An
# unrecognized name must never become a caller-controlled executable path.
BROWSER_SPECS = (
    BrowserSpec("chrome", "Google Chrome", frozenset({"darwin", "linux", "win32"}),
                "chrome", "chrome.exe"),
    BrowserSpec("msedge", "Microsoft Edge", frozenset({"win32"}),
                "msedge", "msedge.exe"),
)


def browser_choices(platform: str) -> tuple[str, ...]:
    return tuple(spec.identifier for spec in BROWSER_SPECS if platform in spec.platforms)


def browser_spec(identifier: str, platform: str) -> BrowserSpec:
    for spec in BROWSER_SPECS:
        if spec.identifier == identifier:
            if platform not in spec.platforms:
                supported = ("Windows" if spec.platforms == frozenset({"win32"})
                             else ", ".join(sorted(spec.platforms)))
                raise ValueError(
                    f"{spec.display_name} Subchat login is supported on {supported} only")
            return spec
    raise ValueError(f"Unsupported Subchat browser: {identifier}")
