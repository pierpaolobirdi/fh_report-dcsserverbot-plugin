"""Map line under "Front Status": read from DCSServerBot, remembered per instance,
replaced when DCSSB reports another map, shown nothing when never known."""
import asyncio
import json
import os
import shutil

from conftest import FIXTURES, commands, make_plugin
from test_update_server import _Bot, _Channel, _Server


class _Mission:
    def __init__(self, map_name):
        self.map = map_name


class _MapServer(_Server):
    def __init__(self, mission=None):
        super().__init__()
        self.current_mission = mission


class _BrokenMissionServer(_Server):
    @property
    def current_mission(self):
        raise RuntimeError("mission not ready")


def _plugin(tmp_path, channel, maps_file=None):
    return make_plugin(bot=_Bot(channel), _message_ids_file=str(tmp_path / "ids.json"),
                       _last_maps_file=str(maps_file or tmp_path / "last_maps.json"))


def _cycle(plugin, server, saves, layout="R"):
    asyncio.run(plugin._update_server(server, {"saves_dir": saves, "channel_id": 5, "campaign_name": "C",
                                               "report_layout": layout}))


def _saves(tmp_path, with_save=True):
    d = tmp_path / "Saves"
    d.mkdir()
    shutil.copy(os.path.join(FIXTURES, "ranks_new.lua"), d / "Foothold_Ranks.lua")
    if with_save:
        shutil.copy(os.path.join(FIXTURES, "foothold_new.lua"), d / "foothold_x.lua")
    return str(d)


def _map_line(channel):
    lines = channel.sent.description.split("\n")
    return [ln for ln in lines if "Map:" in ln]


def test_map_line_sits_under_front_status(tmp_path):
    ch = _Channel({})
    _cycle(_plugin(tmp_path, ch), _MapServer(_Mission("Syria")), _saves(tmp_path))
    lines = ch.sent.description.split("\n")
    assert lines[0].startswith("**Front Status — ") and lines[1] == "🗺️ Map: Syria" and lines[2] == ""


def test_no_map_known_shows_nothing(tmp_path):
    saves = _saves(tmp_path)
    for i, server in enumerate((_MapServer(None), _MapServer(_Mission("")), _MapServer(_Mission(None)),
                                _BrokenMissionServer())):
        ch = _Channel({})
        _cycle(_plugin(tmp_path, ch, tmp_path / f"maps{i}.json"), server, saves)
        assert _map_line(ch) == [] and ch.sent.description.split("\n")[1] == ""


def test_last_known_map_is_kept_when_no_mission_is_loaded(tmp_path):
    saves, ch = _saves(tmp_path), _Channel({})
    plugin = _plugin(tmp_path, ch)
    _cycle(plugin, _MapServer(_Mission("Syria")), saves)
    _cycle(plugin, _MapServer(None), saves)                    # server stopped / mission unloading
    assert _map_line(ch) == ["🗺️ Map: Syria"]


def test_map_is_replaced_when_dcssb_reports_another(tmp_path):
    saves, ch = _saves(tmp_path), _Channel({})
    plugin = _plugin(tmp_path, ch)
    _cycle(plugin, _MapServer(_Mission("Syria")), saves)
    _cycle(plugin, _MapServer(_Mission("Caucasus")), saves)
    assert _map_line(ch) == ["🗺️ Map: Caucasus"]
    assert json.load(open(tmp_path / "last_maps.json")) == {"inst": "Caucasus"}


def test_last_map_survives_a_restart(tmp_path):
    saves, ch = _saves(tmp_path), _Channel({})
    _cycle(_plugin(tmp_path, ch), _MapServer(_Mission("Syria")), saves)
    restarted = _plugin(tmp_path, ch)
    restarted._last_maps = restarted._load_last_maps()          # what cog_load does
    _cycle(restarted, _MapServer(None), saves)                  # DCSSB doesn't know the map yet
    assert _map_line(ch) == ["🗺️ Map: Syria"]


def test_corrupt_or_odd_state_file_is_ignored(tmp_path):
    for content in ("not json", "[1, 2]", '{"inst": 5, "ok": "Syria", "empty": ""}'):
        f = tmp_path / "last_maps.json"
        f.write_text(content)
        plugin = _plugin(tmp_path, _Channel({}))
        loaded = plugin._load_last_maps()
        assert loaded in ({}, {"ok": "Syria"})


def test_maps_are_kept_per_instance(tmp_path):
    saves, ch = _saves(tmp_path), _Channel({})
    plugin = _plugin(tmp_path, ch)
    other = _MapServer(_Mission("Kola"))
    other.instance = type("I", (), {"name": "other"})()
    assert plugin._known_map(_MapServer(_Mission("Syria"))) == "Syria"
    assert plugin._known_map(other) == "Kola"
    assert plugin._known_map(_MapServer(None)) == "Syria"


def test_not_started_embed_also_shows_the_map(tmp_path):
    ch = _Channel({})
    _cycle(_plugin(tmp_path, ch), _MapServer(_Mission("Syria")), _saves(tmp_path, with_save=False))
    assert "Campaign not started" in ch.sent.description and _map_line(ch) == ["🗺️ Map: Syria"]


def test_embed_without_map_is_unchanged():
    embed = commands.build_embed({"blue": [], "red": [], "neutral": 0}, {}, {"campaign_name": "C"})
    assert "Map:" not in embed.description
