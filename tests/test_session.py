"""Campaign session start: does a mission change / a campaign restarted from zero get noticed?"""
import asyncio
import json
import os
import shutil
from datetime import datetime, timedelta

from conftest import FIXTURES, FileNode, commands, make_plugin

T0 = datetime(2026, 10, 3, 12, 0, 0)
D0 = datetime(2026, 10, 3, 0, 0, 0)     # a campaign first seen at T0 counts from that day's reset
IDS = [f"{i:032x}" for i in range(1, 6)]


class _Cur:
    def __init__(self, pool):
        self.pool = pool

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass

    async def execute(self, query, args=None):
        self.pool.queries.append((" ".join(query.split()), args))

    async def fetchone(self):
        return (self.pool.mission_start,)

    async def fetchall(self):
        return []


class _Conn(_Cur):
    def cursor(self):
        return _Cur(self.pool)


class _Pool:
    def __init__(self):
        self.mission_start, self.queries = None, []

    def connection(self):
        return _Conn(self)


class _Server:
    name = "Public"
    status = None

    def __init__(self):
        self.node = FileNode()
        self.instance = type("I", (), {"name": "inst"})()


class _Clock:
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


def _points(factor=1.0, ids=IDS[:3]):
    return {i: 1000 * factor * (n + 1) for n, i in enumerate(ids)}, {i: 10 * (n + 1) if factor >= 1 else 0 for n, i in enumerate(ids)}


class _Game:
    """One saves folder watched by one plugin, cycle after cycle."""

    def __init__(self, tmp_path, monkeypatch):
        self.dir, self.pool = tmp_path, _Pool()
        self.plugin = make_plugin(apool=self.pool)
        self.clock = _Clock(monkeypatch)
        self.source = FileNode()

    def cycle(self, minutes, points, kills, name="foothold_x.lua"):
        self.clock.at(minutes)
        node = commands._UpdateReadCache(self.source)
        return asyncio.run(self.plugin._update_session(
            _Server(), str(self.dir), node, self.source, str(self.dir / name), points, kills))

    def state(self):
        return json.loads((self.dir / ".fhc" / "fhr_session.json").read_text())

    def restart_bot(self):
        self.plugin = make_plugin(apool=self.pool)


def test_first_look_and_normal_play_keep_the_start(tmp_path, monkeypatch):
    g = _Game(tmp_path, monkeypatch)
    pts, kills = _points()
    assert g.cycle(0, pts, kills) == D0
    grown = {i: v + 500 for i, v in pts.items()}
    assert g.cycle(5, grown, {i: v + 2 for i, v in kills.items()}) == D0
    assert g.cycle(10, {**grown, IDS[4]: 100}, kills) == D0          # a new pilot joins


def test_mission_change_to_another_map_is_noticed_at_once(tmp_path, monkeypatch):
    g = _Game(tmp_path, monkeypatch)
    pts, kills = _points()
    g.cycle(0, pts, kills)
    g.pool.mission_start = T0 + timedelta(minutes=6)               # DCSSB loaded the new mission
    start = g.cycle(10, {}, {}, name="FootHold_CA_v0.3.lua")
    assert start == T0 + timedelta(minutes=6)
    assert g.state()["file"] == "FootHold_CA_v0.3.lua" and g.state()["kind"] == "detected"
    assert g.cycle(15, *_points(0.3), name="FootHold_CA_v0.3.lua") == start      # stays put afterwards


def test_campaign_restarted_from_zero_in_place_needs_a_second_look(tmp_path, monkeypatch):
    """Foothold wins, empties the same file and reloads the mission."""
    g = _Game(tmp_path, monkeypatch)
    pts, kills = _points()
    g.cycle(0, pts, kills)
    g.cycle(5, pts, kills)
    g.pool.mission_start = T0 + timedelta(minutes=7)
    assert g.cycle(10, {}, {}) == D0                                # first look: could be a glitch
    assert g.cycle(15, {}, {}) == T0 + timedelta(minutes=7)         # second look confirms
    assert g.state()["kind"] == "detected"
    since, until = g.pool.queries[-1][1][1:]
    assert (since, until) == (T0 + timedelta(minutes=5), T0 + timedelta(minutes=15))   # last seen intact


def test_restarted_campaign_with_new_pilots_already_flying_is_noticed(tmp_path, monkeypatch):
    g = _Game(tmp_path, monkeypatch)
    pts, kills = _points()
    g.cycle(0, pts, kills)
    low = {i: v * 0.1 for i, v in pts.items()}                     # same pilots, points back near zero
    g.cycle(5, low, {i: 0 for i in kills})
    assert g.cycle(10, low, {i: 1 for i in kills}) != D0


def test_a_single_bad_read_is_not_a_restart(tmp_path, monkeypatch):
    """The save is rewritten in place every minute; a read can catch it empty."""
    g = _Game(tmp_path, monkeypatch)
    pts, kills = _points()
    g.cycle(0, pts, kills)
    g.cycle(5, {}, {})                                              # caught half-written
    assert g.cycle(10, pts, kills) == D0                            # normal again
    assert g.cycle(15, {}, {}) == D0                                # the glitch has to repeat to count


def test_pilots_leaving_or_a_small_loss_is_not_a_restart(tmp_path, monkeypatch):
    g = _Game(tmp_path, monkeypatch)
    pts, kills = _points()
    g.cycle(0, pts, kills)
    one_left = {IDS[0]: pts[IDS[0]]}
    for minute in (5, 10):
        assert g.cycle(minute, one_left, {IDS[0]: kills[IDS[0]]}) == D0
    penalised = {i: v - 500 for i, v in pts.items()}                # friendly-fire penalty
    assert g.cycle(15, penalised, kills) == D0


def test_admin_deleting_the_tracking_files_starts_a_new_session(tmp_path, monkeypatch):
    g = _Game(tmp_path, monkeypatch)
    pts, kills = _points()
    g.cycle(0, pts, kills)
    g.plugin._campaign_absent.add(str(tmp_path))                    # "campaign not started" was shown
    g.plugin._campaign_watch.pop(str(tmp_path), None)
    g.pool.mission_start = T0 + timedelta(minutes=8)
    assert g.cycle(10, *_points(0.2)) == T0 + timedelta(minutes=8)
    assert not g.plugin._campaign_absent


def test_no_mission_restart_in_between_dates_it_when_noticed(tmp_path, monkeypatch):
    g = _Game(tmp_path, monkeypatch)
    g.cycle(0, *_points())
    g.plugin._campaign_absent.add(str(tmp_path))
    g.pool.mission_start = None
    assert g.cycle(10, {}, {}) == T0 + timedelta(minutes=10)


def test_bot_restart_keeps_the_start_and_still_notices_a_later_reset(tmp_path, monkeypatch):
    g = _Game(tmp_path, monkeypatch)
    pts, kills = _points()
    g.cycle(0, pts, kills)
    g.cycle(20, pts, kills)                                         # baseline written to disk
    g.restart_bot()                                                 # memory gone
    assert g.cycle(25, pts, kills) == D0
    g.pool.mission_start = T0 + timedelta(minutes=27)
    g.cycle(30, {}, {})
    assert g.cycle(35, {}, {}) == T0 + timedelta(minutes=27)


def test_reset_while_the_bot_was_down_is_noticed_from_the_stored_baseline(tmp_path, monkeypatch):
    g = _Game(tmp_path, monkeypatch)
    pts, kills = _points()
    g.cycle(0, pts, kills)
    g.cycle(20, pts, kills)
    g.restart_bot()
    g.pool.mission_start = T0 + timedelta(minutes=40)
    g.cycle(60, {}, {})
    assert g.cycle(65, {}, {}) != D0


def test_state_from_the_creation_time_experiment_is_migrated(tmp_path, monkeypatch):
    g = _Game(tmp_path, monkeypatch)
    (tmp_path / ".fhc").mkdir()
    (tmp_path / ".fhc" / "fhr_session.json").write_text(json.dumps({
        "file": "foothold_x.lua", "start": "2026-01-01T00:00:00", "kind": "exact",
        "fallback": "2026-10-03T09:00:00", "fallback_kind": "first_seen", "probe": 1}))
    assert g.cycle(0, *_points()) == D0      # first seen 09:00 -> counted from the day start


def test_baseline_is_written_rarely(tmp_path, monkeypatch):
    g = _Game(tmp_path, monkeypatch)
    pts, kills = _points()
    g.cycle(0, pts, kills)
    g.cycle(5, {i: v + 1 for i, v in pts.items()}, kills)
    assert g.state()["seen"] == "2026-10-03T12:00:00"
    g.cycle(16, pts, kills)
    assert g.state()["seen"] == "2026-10-03T12:16:00"


# ── through _update_server, with real save files ────────────────────────────────

class _Chan:
    def get_partial_message(self, mid):
        chan = self

        class _P:
            async def edit(self, embed=None):
                chan.last = embed
        return _P()

    async def send(self, embed=None):
        self.last = embed
        return type("M", (), {"id": 1, "author": None, "embeds": []})()

    def history(self, limit=50):
        async def gen():
            if False:
                yield
        return gen()


def _full_cycle(plugin, d, minutes, clock):
    clock.at(minutes)
    server = _Server()
    chan = _Chan()
    plugin.bot = type("B", (), {"get_channel": lambda self, _: chan, "user": None})()
    asyncio.run(plugin._update_server(server, {"saves_dir": d, "channel_id": 5, "campaign_name": "C", "report_layout": "R"}))


def _state(d):
    return json.loads(open(os.path.join(d, ".fhc", "fhr_session.json")).read())


def test_update_server_follows_a_real_campaign_reset_and_a_map_change(tmp_path, monkeypatch):
    d = str(tmp_path)
    shutil.copy(os.path.join(FIXTURES, "ranks_new.lua"), tmp_path / "Foothold_Ranks.lua")
    shutil.copy(os.path.join(FIXTURES, "foothold_new.lua"), tmp_path / "foothold_afghanistan.lua")
    pool, clock = _Pool(), _Clock(monkeypatch)
    plugin = make_plugin(apool=pool, _message_ids_file=str(tmp_path / "ids.json"))

    _full_cycle(plugin, d, 0, clock)
    first = _state(d)
    assert first["kind"] == "first_seen" and first["file"] == "foothold_afghanistan.lua" and first["base"]["points"]
    _full_cycle(plugin, d, 5, clock)
    assert _state(d)["start"] == first["start"]                     # normal play: untouched

    # the campaign is won: Foothold empties the same file, the mission reloads
    (tmp_path / "foothold_afghanistan.lua").write_text("zonePersistance = {}\n")
    pool.mission_start = T0 + timedelta(minutes=11)
    _full_cycle(plugin, d, 10, clock)
    assert _state(d)["start"] == first["start"]                     # first look only
    _full_cycle(plugin, d, 15, clock)
    assert _state(d)["start"] == "2026-10-03T12:11:00" and _state(d)["kind"] == "detected"

    # the admin switches to another map: other tracking file
    os.remove(tmp_path / "foothold_afghanistan.lua")
    shutil.copy(os.path.join(FIXTURES, "foothold_new.lua"), tmp_path / "FootHold_CA_v0.3.lua")
    pool.mission_start = T0 + timedelta(minutes=21)
    _full_cycle(plugin, d, 25, clock)
    assert _state(d)["file"] == "FootHold_CA_v0.3.lua" and _state(d)["start"] == "2026-10-03T12:21:00"

    # the admin deletes the tracking files: "not started", then they come back
    os.remove(tmp_path / "FootHold_CA_v0.3.lua")
    _full_cycle(plugin, d, 30, clock)
    shutil.copy(os.path.join(FIXTURES, "foothold_new.lua"), tmp_path / "FootHold_CA_v0.3.lua")
    pool.mission_start = None
    _full_cycle(plugin, d, 40, clock)
    assert _state(d)["start"] == "2026-10-03T12:40:00"


def test_a_campaign_first_seen_counts_at_least_the_day_it_was_seen_in():
    """The session is never narrower than the day for a campaign that was already running."""
    seen = {"start": "2026-10-03T18:30:00", "kind": "first_seen"}
    assert commands._session_start_for(seen, {"daily_reset_hour": 6}) == datetime(2026, 10, 3, 6, 0)
    early = {"start": "2026-10-03T03:00:00", "kind": "first_seen"}         # before today's reset
    assert commands._session_start_for(early, {"daily_reset_hour": 6}) == datetime(2026, 10, 2, 6, 0)
    sat = {"start": "2026-10-03T18:30:00", "kind": "first_seen"}            # a Saturday with its own hour
    assert commands._session_start_for(sat, {"daily_reset_hour": 6, "daily_reset_schedule": {"sat": 9}}) \
        == datetime(2026, 10, 3, 9, 0)
    # a reset that was actually noticed is the real start, even if it is later than the day's reset
    assert commands._session_start_for({"start": "2026-10-03T15:00:00", "kind": "detected"}, {}) \
        == datetime(2026, 10, 3, 15, 0)
    assert commands._session_start_for({}, {}) is None


def test_update_session_returns_the_widened_start_for_a_campaign_first_seen(tmp_path, monkeypatch):
    g = _Game(tmp_path, monkeypatch)
    g.clock.at(0)
    node = commands._UpdateReadCache(g.source)
    start = asyncio.run(g.plugin._update_session(_Server(), str(tmp_path), node, g.source, str(tmp_path / "f.lua"),
                                                 {IDS[0]: 100}, {IDS[0]: 1}, {"daily_reset_hour": 4}))
    assert start == datetime(2026, 10, 3, 4, 0)
    assert g.state()["start"] == "2026-10-03T12:00:00"                     # what was stored is the first look
