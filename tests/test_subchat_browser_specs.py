"""Dedicated Subchat browsers are selected by a closed product registry."""

import pytest

from anywhere_computer.subchat_browser_specs import browser_choices, browser_spec


def test_browser_registry_preserves_supported_platforms_and_launch_products():
    assert browser_choices("darwin") == ("chrome",)
    assert browser_choices("linux") == ("chrome",)
    assert browser_choices("win32") == ("chrome", "msedge")
    assert browser_spec("chrome", "win32").windows_executable == "chrome.exe"
    assert browser_spec("msedge", "win32").windows_executable == "msedge.exe"
    assert browser_spec("msedge", "win32").playwright_channel == "msedge"


def test_browser_registry_rejects_other_engines_and_unverified_platforms():
    with pytest.raises(ValueError, match="Unsupported Subchat browser"):
        browser_spec("firefox", "win32")
    with pytest.raises(ValueError, match="Unsupported Subchat browser"):
        browser_spec("../chrome.exe", "win32")
    with pytest.raises(ValueError, match="Windows only"):
        browser_spec("msedge", "darwin")
