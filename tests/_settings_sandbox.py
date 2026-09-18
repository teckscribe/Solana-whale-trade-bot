"""
Redirects settings_manager at a throwaway copy of data/settings.json so tests that call
settings_manager.update() cannot alter the bot's live configuration.
"""

import os
import shutil
import tempfile

import settings_manager

_state = {}


def enter():
    tmp = tempfile.mkdtemp(prefix="wtb_test_settings_")
    src = settings_manager.SETTINGS_FILE
    dst = os.path.join(tmp, "settings.json")
    if os.path.exists(src):
        shutil.copy(src, dst)
    _state.update(
        tmp=tmp,
        settings=settings_manager.SETTINGS_FILE,
        history=settings_manager.HISTORY_FILE,
        data_dir=settings_manager.DATA_DIR,
        cache=dict(settings_manager._cache),
    )
    settings_manager.DATA_DIR = tmp
    settings_manager.SETTINGS_FILE = dst
    settings_manager.HISTORY_FILE = os.path.join(tmp, "settings_history.jsonl")
    settings_manager._cache.update(key=None, checked=0.0, raw={}, typed={})


def exit_():
    if not _state:
        return
    settings_manager.DATA_DIR = _state["data_dir"]
    settings_manager.SETTINGS_FILE = _state["settings"]
    settings_manager.HISTORY_FILE = _state["history"]
    settings_manager._cache.clear()
    settings_manager._cache.update(_state["cache"])
    settings_manager._cache.update(key=None, checked=0.0)
    shutil.rmtree(_state["tmp"], ignore_errors=True)
    _state.clear()
