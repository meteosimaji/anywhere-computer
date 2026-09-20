"""Simulate a Windows legacy default encoding without changing global locale."""
import importlib
from pathlib import Path

import pytest


def test_browser_assets_ignore_legacy_default_encoding(monkeypatch):
    pytest.importorskip('playwright.async_api')
    original = Path.open

    def legacy_open(path, mode='r', buffering=-1, encoding=None, errors=None, newline=None):
        if path.parent.name == 'subchat_browser' and path.suffix == '.js' and 'b' not in mode:
            encoding = encoding or 'cp1252'
        return original(path, mode, buffering, encoding, errors, newline)

    monkeypatch.setattr(Path, 'open', legacy_open)
    from anywhere_computer.subchat_browser import backend, catalog

    importlib.reload(backend)
    importlib.reload(catalog)
    for name, source in [('subchat_copy.js', backend.COPY), ('subchat_input.js', backend.INPUT),
                         ('subchat_model_menu.js', catalog.SOURCE)]:
        expected = Path(backend.__file__).with_name(name).read_bytes().decode('utf-8')
        assert source == expected
