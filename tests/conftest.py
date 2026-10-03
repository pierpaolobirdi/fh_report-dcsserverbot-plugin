"""Test setup: FH_Report runs against small stubs of discord.py and
DCSServerBot (tests/stubs), so no bot, Discord or database is needed.

    python -m pytest tests

Golden files (tests/golden/) pin the exact embed / daily-tracking output.
After an intentional output change, regenerate them with:

    FH_UPDATE_GOLDEN=1 python -m pytest tests
"""
import hashlib
import json
import logging
import os
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


class FileNode:
    """Stand-in for a DCSServerBot node backed by the local filesystem.
    No write_file(), so writes take the local fallback path."""

    def __init__(self):
        self.reads: list[str] = []

    async def read_file(self, path):
        self.reads.append(os.path.basename(path))
        with open(path, "rb") as f:
            return f.read()

    async def list_directory(self, path):
        return os.listdir(path)


def make_plugin(**attrs):
    p = commands.FH_Report.__new__(commands.FH_Report)
    p.log = logging.getLogger("fh_report.tests")
    p._message_ids, p._layout_cycle_index, p._cycle_punishment = {}, {}, None
    p._last_maps, p._map_probed = {}, set()
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
