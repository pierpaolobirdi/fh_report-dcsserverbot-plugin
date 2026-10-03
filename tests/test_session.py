"""Campaign session start: first sight, resets, and narrowing to DCSSB's mission start."""
import asyncio
import json
from datetime import datetime, timedelta

from conftest import FileNode, commands, make_plugin

T0 = datetime(2026, 10, 3, 12, 0, 0)


class _Cur:
    def __init__(self, pool):
        self.pool = pool

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass

    async def execute(self, query, args):
        self.pool.queries.append((query, args))

    async def fetchone(self):
        return (self.pool.mission_start,)


class _Conn(_Cur):
    def cursor(self):
        return _Cur(self.pool)


class _Pool:
    def __init__(self, mission_start=None):
        self.mission_start, self.queries = mission_start, []

    def connection(self):
        return _Conn(self)


class _Server:
    name = "Public"


def _plugin(pool=None):
    p = make_plugin(apool=pool or _Pool())
    return p


def _run(plugin, tmp_path, now, reset=False, name="foothold_x.lua", monkeypatch=None):
    source = FileNode()
    node = commands._UpdateReadCache(source)
    return asyncio.run(plugin._update_session(_Server(), str(tmp_path), node, source,
                                              str(tmp_path / name), reset=reset))


def _state(tmp_path):
    return json.loads((tmp_path / ".fhc" / "fhr_session.json").read_text())


class _Clock:
    """Fixes the 'now' the updater sees."""
    def __init__(self, monkeypatch):
        self.now = T0
        outer = self

        class _DT(datetime):
            @classmethod
            def now(cls, tz=None):
                return outer.now.replace(tzinfo=tz) if tz else outer.now
        monkeypatch.setattr(commands, "datetime", _DT)

    def at(self, minutes):
        self.now = T0 + timedelta(minutes=minutes)


def test_a_campaign_already_running_is_first_seen(tmp_path, monkeypatch):
    clock = _Clock(monkeypatch)
    got = _run(_plugin(), tmp_path, clock.now)
    assert got["kind"] == "first_seen" and got["start"] == T0
    assert _state(tmp_path)["start"] == "2026-10-03T12:00:00"


def test_nothing_happening_keeps_the_start(tmp_path, monkeypatch):
    clock, plugin = _Clock(monkeypatch), _plugin()
    _run(plugin, tmp_path, clock.now)
    clock.at(5)
    got = _run(plugin, tmp_path, clock.now)
    assert got["start"] == T0 and got["kind"] == "first_seen"


def test_reset_in_place_is_detected_at_the_mission_restart(tmp_path, monkeypatch):
    """Foothold empties the same file and reloads the mission seconds later: the
    session starts at that mission start, not when the next cycle noticed."""
    clock = _Clock(monkeypatch)
    restart = T0 + timedelta(minutes=7)
    plugin = _plugin(_Pool(mission_start=restart))
    _run(plugin, tmp_path, clock.now)
    clock.at(5)
    _run(plugin, tmp_path, clock.now)
    clock.at(10)                                            # reset noticed 3 min after the restart
    got = _run(plugin, tmp_path, clock.now, reset=True)
    assert got == {"start": restart, "kind": "detected"}
    sql, args = plugin.apool.queries[-1]
    assert args == ("Public", T0 + timedelta(minutes=5), T0 + timedelta(minutes=10))   # since last seen intact


def test_reset_without_a_mission_restart_starts_when_noticed(tmp_path, monkeypatch):
    clock = _Clock(monkeypatch)
    plugin = _plugin(_Pool(mission_start=None))             # admin deleted the files, no reload
    _run(plugin, tmp_path, clock.now)
    clock.at(5)
    assert _run(plugin, tmp_path, clock.now, reset=True)["start"] == clock.now


def test_a_wide_gap_is_not_narrowed(tmp_path, monkeypatch):
    """Bot down for hours: any mission start in that gap could be an ordinary restart."""
    clock = _Clock(monkeypatch)
    plugin = _plugin(_Pool(mission_start=T0 + timedelta(hours=1)))
    _run(plugin, tmp_path, clock.now)
    plugin._campaign_seen.clear()                           # as after a bot restart
    clock.at(60 * 5)
    got = _run(plugin, tmp_path, clock.now, reset=True)
    assert got["start"] == clock.now and not plugin.apool.queries


def test_new_save_file_name_and_save_reappearing_are_new_sessions(tmp_path, monkeypatch):
    clock, plugin = _Clock(monkeypatch), _plugin()
    _run(plugin, tmp_path, clock.now)
    clock.at(5)
    assert _run(plugin, tmp_path, clock.now, name="FootHold_CA_v0.3.lua")["kind"] == "detected"
    clock.at(10)
    plugin._campaign_absent.add(str(tmp_path))              # "campaign not started" was shown in between
    got = _run(plugin, tmp_path, clock.now, name="FootHold_CA_v0.3.lua")
    assert got["kind"] == "detected" and got["start"] == clock.now and not plugin._campaign_absent


def test_state_written_by_the_creation_time_probe_is_migrated(tmp_path, monkeypatch):
    """14.1.28 stored a (wrong) file creation date as an 'exact' start."""
    clock, plugin = _Clock(monkeypatch), _plugin()
    (tmp_path / ".fhc").mkdir()
    (tmp_path / ".fhc" / "fhr_session.json").write_text(json.dumps({
        "file": "foothold_x.lua", "start": "2026-01-01T00:00:00", "kind": "exact",
        "fallback": "2026-10-03T09:00:00", "fallback_kind": "first_seen", "probe": 1, "verified": True, "bad": False}))
    got = _run(plugin, tmp_path, clock.now)
    assert got == {"start": datetime(2026, 10, 3, 9, 0, 0), "kind": "first_seen"}


def test_seen_is_written_to_disk_only_every_so_often(tmp_path, monkeypatch):
    clock, plugin = _Clock(monkeypatch), _plugin()
    _run(plugin, tmp_path, clock.now)
    clock.at(5)
    _run(plugin, tmp_path, clock.now)
    assert _state(tmp_path)["seen"] == "2026-10-03T12:00:00"      # not rewritten every cycle
    clock.at(16)
    _run(plugin, tmp_path, clock.now)
    assert _state(tmp_path)["seen"] == "2026-10-03T12:16:00"
