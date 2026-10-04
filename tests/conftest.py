"""Test setup: Fh_Report runs against small stubs of discord.py and
DCSServerBot (tests/stubs), so no bot, Discord or database is needed.

    python -m pytest tests

Golden files (tests/golden/) pin the exact embed / daily-tracking output.
After an intentional output change, regenerate them with:

    FH_UPDATE_GOLDEN=1 python -m pytest tests
"""
import enum
import glob
import hashlib
import json
import logging
import os
import pathlib
import shutil
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [os.path.join(ROOT, "tests", "stubs"), os.path.join(ROOT, "plugins")]

from fh_report import commands  # noqa: E402

FIXTURES = os.path.join(ROOT, "tests", "fixtures")
GOLDEN   = os.path.join(ROOT, "tests", "golden")
UPDATE_GOLDEN = os.environ.get("FH_UPDATE_GOLDEN") == "1"
UCID = [f"{i:032x}" for i in range(100)]


class UploadStatus(enum.Enum):
    OK = 0
    FILE_EXISTS = 1
    READ_ERROR = 2
    WRITE_ERROR = 3


class FileNode:
    """Stand-in for a local DCSServerBot node (NodeImpl), behaving like the real one:
    list_directory returns (directory, full paths, newest first) and does NOT fail for a
    missing folder; write_file copies a local file and does NOT create folders;
    remove_file treats its argument as a glob pattern."""

    def __init__(self):
        self.reads: list[str] = []

    async def read_file(self, path):
        self.reads.append(os.path.basename(path))
        with open(path, "rb") as f:
            return f.read()

    async def write_file(self, target, source, overwrite=False):
        if os.path.exists(target) and not overwrite:
            return UploadStatus.FILE_EXISTS
        try:
            shutil.copy2(source, target)
            return UploadStatus.OK
        except Exception:
            return UploadStatus.WRITE_ERROR

    async def list_directory(self, path, *, pattern="*", order=None, is_dir=False, ignore=None, traverse=False):
        directory = pathlib.Path(os.path.expandvars(path))
        patterns = [pattern] if isinstance(pattern, str) else pattern
        found = []
        for pat in patterns:
            for f in (directory.rglob(pat) if traverse else directory.glob(pat)):
                if f.name in (ignore or []):
                    continue
                if (f.is_dir() and is_dir) or (not is_dir and not f.is_dir()):
                    found.append(f)
        found.sort(key=os.path.getmtime, reverse=True)
        return directory.as_posix(), [f.as_posix() for f in found]

    async def create_directory(self, path):
        os.makedirs(path, exist_ok=True)

    async def remove_file(self, path):
        for f in glob.glob(path):
            os.remove(f)


def make_plugin(**attrs):
    p = commands.Fh_Report.__new__(commands.Fh_Report)
    p.log = logging.getLogger("fh_report.tests")
    p._message_ids, p._layout_cycle_index, p._cycle_punishment = {}, {}, None
    p._last_maps, p._map_probed = {}, set()
    p._campaign_absent, p._campaign_watch = set(), {}
    p._base_interval, p._beat, p._beats_left, p._last_status = 300, 300, {}, {}
    p._reset_handled = {}
    for k, v in attrs.items():
        setattr(p, k, v)
    return p


def embed_dump(embed) -> list:
    """Embed content, minus the current time and the release number."""
    import re
    footer = embed.footer.text.replace(commands.FH_REPORT_RELEASE, "X") if embed.footer else None
    return ([embed.title, re.sub(r"<t:\d+:f>", "T", embed.description or "")]
            + [[f.name, f.value, f.inline] for f in embed.fields] + [footer])


def digest(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()


def check_golden(name: str, results: dict) -> None:
    path = os.path.join(GOLDEN, name)
    if UPDATE_GOLDEN:
        os.makedirs(GOLDEN, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=1, sort_keys=True)
        return
    with open(path, encoding="utf-8") as f:
        expected = json.load(f)
    changed = sorted(k for k in expected.keys() | results.keys() if expected.get(k) != results.get(k))
    assert not changed, f"{len(changed)} case(s) differ from {name}, e.g. {changed[:5]}"


@pytest.fixture(autouse=True)
def _fresh_run_state():
    """Module-level 'already done this run' markers must not leak between tests."""
    commands._legacy_cleaned.clear()
    commands._interval_warned.clear()
    commands._missing_save_warned.clear()
    yield


@pytest.fixture
def saves_dir(tmp_path):
    """A saves folder holding copies of the given fixture files."""
    def _make(ranks="ranks_new.lua", campaign="foothold_new.lua"):
        if ranks:
            shutil.copy(os.path.join(FIXTURES, ranks), tmp_path / "Foothold_Ranks.lua")
        if campaign:
            shutil.copy(os.path.join(FIXTURES, campaign), tmp_path / "foothold_x.lua")
        return str(tmp_path)
    return _make
