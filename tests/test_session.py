"""Campaign session start: state machine, plausibility checks and the probe flow."""
import asyncio
import json
import os
import shutil
import subprocess
from datetime import datetime

import pytest

from conftest import FileNode, commands, make_plugin

NOW = datetime(2026, 10, 3, 18, 0, 0)
EPOCH = int(datetime(2026, 10, 3, 17, 0, 0, tzinfo=commands.timezone.utc).timestamp())   # an hour before NOW


def test_first_sight_is_first_seen_and_a_new_file_is_detected():
    st = commands._session_next({}, "foothold_a.lua", NOW, reset=False)
    assert st["kind"] == "first_seen" and st["start"] == "2026-10-03T18:00:00"
    later = datetime(2026, 10, 3, 19, 0, 0)
    assert commands._session_next(st, "foothold_a.lua", later, reset=False) is st            # nothing happened
    assert commands._session_next(st, "foothold_b.lua", later, reset=False)["kind"] == "detected"   # other file
    assert commands._session_next(st, "foothold_a.lua", later, reset=True)["start"] == "2026-10-03T19:00:00"


def test_probe_makes_the_start_exact_then_verifies_it():
    st = commands._new_session("foothold_a.lua", NOW, "first_seen")
    st = commands._session_apply_probe(st, EPOCH, NOW)
    assert st["kind"] == "exact" and st["start"] == "2026-10-03T17:00:00" and not st["verified"]
    st = commands._session_apply_probe(st, EPOCH, NOW)            # same answer again: trusted
    assert st["verified"] and st["kind"] == "exact" and not st["bad"]


def test_probe_that_changes_between_readings_is_rejected():
    """A system reporting the last write instead of the creation moves on every save."""
    st = commands._session_apply_probe(commands._new_session("f.lua", NOW, "detected"), EPOCH, NOW)
    st = commands._session_apply_probe(st, EPOCH + 300, NOW)
    assert st["bad"] and st["kind"] == "detected" and st["start"] == "2026-10-03T18:00:00"


@pytest.mark.parametrize("created", [0, 500, 4_000_000_000])
def test_implausible_probe_is_ignored(created):
    st = commands._session_apply_probe(commands._new_session("f.lua", NOW, "first_seen"), created, NOW)
    assert st["bad"] and st["kind"] == "first_seen"


def test_probe_later_than_first_sight_is_rejected():
    later_than_seen = int(datetime(2026, 10, 3, 17, 59, 0, tzinfo=commands.timezone.utc).timestamp()) + 3600 * 0
    st = commands._new_session("f.lua", datetime(2026, 10, 3, 12, 0, 0), "first_seen")   # seen at 12:00
    assert commands._session_apply_probe(st, later_than_seen, NOW)["bad"]               # claims creation at 17:59


class _Server:
    def __init__(self, status):
        self.status = status


def _updater(tmp_path, monkeypatch, created, status=commands.Status.RUNNING):
    """Plugin + node where the injected Lua is replaced by a fake that answers `created`."""
    calls = []

    async def fake_do_script(server, lua):
        calls.append(lua)
        out = tmp_path / ".fhc" / "fhr_save_created.json"
        out.parent.mkdir(exist_ok=True)
        out.write_text(json.dumps({"file": "foothold_x.lua", "created": created[0], "modified": created[0]}))

    async def no_sleep(_):
        pass
    monkeypatch.setattr(commands, "_do_script", fake_do_script)
    monkeypatch.setattr(commands.asyncio, "sleep", no_sleep)
    plugin = make_plugin()
    source = FileNode()

    def run(reset=False):
        node = commands._UpdateReadCache(source)
        return asyncio.run(plugin._update_session(_Server(status), str(tmp_path), node, source,
                                                  str(tmp_path / "foothold_x.lua"), reset=reset))
    return plugin, run, calls


def _state(tmp_path):
    return json.loads((tmp_path / ".fhc" / "fhr_session.json").read_text())


def test_updater_goes_first_seen_to_exact_to_verified_and_stops_probing(tmp_path, monkeypatch):
    created = [EPOCH]
    plugin, run, calls = _updater(tmp_path, monkeypatch, created)
    got = run()
    assert got["kind"] == "exact" and len(calls) == 1 and not _state(tmp_path)["verified"]
    run()                                   # second cycle: verification probe
    assert _state(tmp_path)["verified"] and len(calls) == 2
    run()
    assert len(calls) == 2                  # verified: no more injections


def test_a_reset_starts_over_and_probes_again(tmp_path, monkeypatch):
    created = [EPOCH]
    plugin, run, calls = _updater(tmp_path, monkeypatch, created)
    run(); run()
    created[0] = EPOCH + 1800               # the save was recreated 30 min later
    got = run(reset=True)
    assert len(calls) == 3
    assert _state(tmp_path)["kind"] in ("detected", "exact")


def test_save_reappearing_after_the_not_started_screen_is_a_new_session(tmp_path, monkeypatch):
    created = [EPOCH]
    plugin, run, calls = _updater(tmp_path, monkeypatch, created, status=commands.Status.STOPPED)
    first = run()
    assert first["kind"] == "first_seen" and not calls        # mission not running: no probe
    plugin._campaign_absent.add(str(tmp_path))               # the save went missing in between
    assert run()["kind"] == "detected"
    assert str(tmp_path) not in plugin._campaign_absent


def test_unstable_platform_falls_back_to_detection(tmp_path, monkeypatch):
    created = [EPOCH]
    plugin, run, calls = _updater(tmp_path, monkeypatch, created)
    run()
    created[0] = EPOCH + 3600                # next read differs: not a creation time
    got = run()
    assert got["kind"] == "first_seen" and _state(tmp_path)["bad"]
    run()
    assert len(calls) == 2                   # gave up on this file


@pytest.mark.skipif(shutil.which("lua5.1") is None and shutil.which("lua") is None, reason="no Lua interpreter")
def test_injected_lua_runs_and_writes_valid_json(tmp_path):
    out = tmp_path / "out.json"
    src = r"C:\Users\x\Saved Games\DCS\Missions\Saves\foothold_Syria.lua"
    script = ("lfs = {attributes = function(p) return {change = 1790000000, modification = 1790003600} end}\n"
              + commands._save_created_lua(src, str(out)))
    (tmp_path / "t.lua").write_text(script)
    subprocess.run([shutil.which("lua5.1") or shutil.which("lua"), str(tmp_path / "t.lua")], check=True)
    assert json.loads(out.read_text()) == {"file": "foothold_Syria.lua", "created": 1790000000, "modified": 1790003600}
