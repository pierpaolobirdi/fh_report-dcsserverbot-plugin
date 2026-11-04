"""Campaign history: one entry per campaign, with the final season table, kept in .fhc/fhr_campaign_history.json."""
import asyncio
import json
from datetime import timedelta

from conftest import commands, make_plugin
from test_session import IDS, T0, _Clock, _Game, _Server

NAMES = {i: f"Pilot{n}" for n, i in enumerate(IDS)}


def _pts(scale=1.0, ids=IDS[:3]):
    return {i: int(1000 * scale * (n + 1)) for n, i in enumerate(ids)}


class _Hist(_Game):
    def cycle(self, minutes, points, name="foothold_x.lua", map_name="Syria", min_points=200, kills=None):
        self.clock.at(minutes)
        node = commands._UpdateReadCache(self.source)
        kills = kills if kills is not None else {i: 10 for i in points}
        asyncio.run(self.plugin._update_session(
            _Server(), str(self.dir), node, self.source, str(self.dir / name), points, kills))
        asyncio.run(self.plugin._track_campaign(
            _Server(), str(self.dir), self.source, points, NAMES, {"campaign_history_min_points": min_points}, map_name))

    def history(self):
        return json.loads((self.dir / ".fhc" / "fhr_campaign_history.json").read_text())["campaigns"]

    def restart_bot(self):
        self.plugin = make_plugin(apool=self.pool)


def test_the_first_look_opens_an_entry_with_the_whole_season_table(tmp_path, monkeypatch):
    g = _Hist(tmp_path, monkeypatch)
    g.cycle(0, _pts())
    [entry] = g.history()
    assert entry["open"] and entry["file"] == "foothold_x.lua" and entry["map"] == "Syria"
    assert entry["start_kind"] == "first_seen" and entry["end"] is None and entry["total"] == 6000
    assert entry["players"][IDS[2]] == {"name": "Pilot2", "points": 3000}
    assert [r["name"] for r in commands.campaign_ranking(entry)] == ["Pilot2", "Pilot1", "Pilot0"]


def test_a_changed_table_is_written_every_quarter_of_an_hour_not_every_cycle(tmp_path, monkeypatch):
    g = _Hist(tmp_path, monkeypatch)
    g.cycle(0, _pts())
    g.cycle(5, _pts(2))
    assert g.history()[0]["total"] == 6000                         # held back
    g.cycle(16, _pts(3))
    assert g.history()[0]["total"] == 18000


def test_a_map_change_closes_the_campaign_with_its_last_table_and_opens_the_next(tmp_path, monkeypatch):
    g = _Hist(tmp_path, monkeypatch)
    g.cycle(0, _pts())
    g.cycle(5, _pts(2))
    g.pool.mission_start = T0 + timedelta(minutes=6)
    g.cycle(10, {}, name="FootHold_CA_v0.3.lua", map_name="Caucasus")
    old, new = g.history()
    assert not old["open"] and old["closed_by"] == "map" and old["total"] == 12000
    assert old["end"] == (T0 + timedelta(minutes=6)).strftime("%Y-%m-%dT%H:%M:%S")
    assert new["open"] and new["file"] == "FootHold_CA_v0.3.lua" and new["map"] == "Caucasus"


def test_coming_back_to_a_map_without_losing_points_reopens_its_entry(tmp_path, monkeypatch):
    g = _Hist(tmp_path, monkeypatch)
    g.cycle(0, _pts())
    g.cycle(10, _pts(0.2, IDS[3:5]), name="foothold_y.lua", map_name="Caucasus", min_points=100)
    g.cycle(20, _pts(1.5), min_points=100)                          # Syria again, points grew
    syria, other = g.history()
    assert len(g.history()) == 2 and syria["open"] and not other["open"]
    assert syria["end"] is None and syria["total"] == 9000 and syria["start_kind"] == "first_seen"


def test_coming_back_to_a_map_that_was_reset_is_a_new_campaign(tmp_path, monkeypatch):
    g = _Hist(tmp_path, monkeypatch)
    g.cycle(0, _pts())
    g.cycle(10, _pts(0.2, IDS[3:5]), name="foothold_y.lua", map_name="Caucasus", min_points=100)
    g.cycle(20, {IDS[0]: 50, IDS[1]: 80}, min_points=100)           # Syria again, started over
    syria_old, other, syria_new = g.history()
    assert not syria_old["open"] and syria_old["total"] == 6000
    assert syria_new["open"] and syria_new["id"] != syria_old["id"]


def test_a_reset_in_the_same_file_closes_with_the_last_intact_table(tmp_path, monkeypatch):
    g = _Hist(tmp_path, monkeypatch)
    g.cycle(0, _pts())
    g.cycle(5, _pts(2))
    g.cycle(10, {})                                                 # first look: could be a glitch, table kept
    assert g.history()[0]["open"] and g.history()[0]["total"] == 6000   # (not written yet: nothing changed)
    g.pool.mission_start = T0 + timedelta(minutes=12)
    g.cycle(15, {})                                                 # second look confirms
    old, new = g.history()
    assert not old["open"] and old["closed_by"] == "reset" and old["total"] == 12000
    assert new["open"] and new["total"] == 0


def test_a_campaign_below_the_minimum_is_not_kept(tmp_path, monkeypatch):
    g = _Hist(tmp_path, monkeypatch)
    g.cycle(0, {IDS[0]: 50, IDS[1]: 60})
    g.cycle(10, _pts(), name="foothold_y.lua", map_name="Caucasus")
    assert [c["file"] for c in g.history()] == ["foothold_y.lua"]


def test_the_history_survives_a_bot_restart(tmp_path, monkeypatch):
    g = _Hist(tmp_path, monkeypatch)
    g.cycle(0, _pts())
    g.restart_bot()
    g.cycle(5, _pts(2))
    g.cycle(20, _pts(3), name="foothold_y.lua", map_name="Caucasus", min_points=100)
    old, new = g.history()
    assert old["id"].startswith("foothold_x@") and not old["open"] and new["open"]


def test_the_excluded_pilots_are_not_in_the_table():
    table = commands._campaign_table({"a": 100, "b": 5, "c": 0}, {"a": "A"}, ["b"])
    assert table == {"a": {"name": "A", "points": 100}}
