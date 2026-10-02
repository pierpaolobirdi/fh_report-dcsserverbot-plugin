"""Map line under "Front Status": read from DCSServerBot, remembered per instance in
saves_dir/.fhc/last_map.json, replaced when DCSSB reports another map, nothing shown
when never known."""
import asyncio
import json
import logging
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


def _plugin(tmp_path, channel):
    return make_plugin(bot=_Bot(channel), _message_ids_file=str(tmp_path / "ids.json"))


def _cycle(plugin, server, saves, layout="R"):
    asyncio.run(plugin._update_server(server, {"saves_dir": saves, "channel_id": 5, "campaign_name": "C",
                                               "report_layout": layout}))


def _map_file(saves):
    return os.path.join(saves, ".fhc", "last_map.json")


def _saves(tmp_path, with_save=True):
    d = tmp_path / "Saves"
    d.mkdir()
    shutil.copy(os.path.join(FIXTURES, "ranks_new.lua"), d / "Foothold_Ranks.lua")
    if with_save:
        shutil.copy(os.path.join(FIXTURES, "foothold_new.lua"), d / "foothold_x.lua")
    return str(d)


def _map_line(channel):
    """Map lines anywhere in the embed's title or description."""
    text = (channel.sent.title or "") + "\n" + (channel.sent.description or "")
    return [ln for ln in text.split("\n") if "🗺️" in ln]


def test_map_is_the_second_line_of_the_title(tmp_path):
    ch = _Channel({})
    _cycle(_plugin(tmp_path, ch), _MapServer(_Mission("Syria")), _saves(tmp_path))
    assert ch.sent.title == "📡  C\n🗺️  Syria"
    assert ch.sent.description.startswith("**Front Status — ")     # directly below, no map line here
    assert "🗺️" not in ch.sent.description


def test_no_map_known_shows_nothing(tmp_path):
    saves = _saves(tmp_path)
    for server in (_MapServer(None), _MapServer(_Mission("")), _MapServer(_Mission(None)),
                   _BrokenMissionServer()):
        ch = _Channel({})
        _cycle(_plugin(tmp_path, ch), server, saves)
        assert _map_line(ch) == [] and ch.sent.title == "📡  C"
    assert not os.path.exists(_map_file(saves))


def test_map_is_stored_in_the_instance_fhc_folder(tmp_path):
    saves = _saves(tmp_path)
    _cycle(_plugin(tmp_path, _Channel({})), _MapServer(_Mission("Syria")), saves)
    assert json.load(open(_map_file(saves))) == {"map": "Syria"}
    assert not [f for f in os.listdir(os.path.dirname(commands.__file__)) if f.startswith("last_map")]


def test_last_known_map_is_kept_when_no_mission_is_loaded(tmp_path):
    saves, ch = _saves(tmp_path), _Channel({})
    plugin = _plugin(tmp_path, ch)
    _cycle(plugin, _MapServer(_Mission("Syria")), saves)
    _cycle(plugin, _MapServer(None), saves)                    # server stopped / mission unloading
    assert _map_line(ch) == ["🗺️  Syria"]


def test_map_is_replaced_when_dcssb_reports_another(tmp_path):
    saves, ch = _saves(tmp_path), _Channel({})
    plugin = _plugin(tmp_path, ch)
    _cycle(plugin, _MapServer(_Mission("Syria")), saves)
    _cycle(plugin, _MapServer(_Mission("Caucasus")), saves)
    assert _map_line(ch) == ["🗺️  Caucasus"]
    assert json.load(open(_map_file(saves))) == {"map": "Caucasus"}


def test_last_map_survives_a_restart(tmp_path):
    saves, ch = _saves(tmp_path), _Channel({})
    _cycle(_plugin(tmp_path, ch), _MapServer(_Mission("Syria")), saves)
    _cycle(_plugin(tmp_path, ch), _MapServer(None), saves)      # fresh plugin: DCSSB doesn't know yet
    assert _map_line(ch) == ["🗺️  Syria"]


def test_file_is_only_rewritten_when_the_map_changes(tmp_path, monkeypatch):
    saves, ch = _saves(tmp_path), _Channel({})
    writes = []
    real = commands.write_bytes_to_node

    async def spy(node, path, data, log=None):
        writes.append(path)
        return await real(node, path, data, log=log)
    monkeypatch.setattr(commands, "write_bytes_to_node", spy)
    plugin = _plugin(tmp_path, ch)
    for _ in range(3):
        _cycle(plugin, _MapServer(_Mission("Syria")), saves)
    assert [w for w in writes if w.endswith("last_map.json")] == [_map_file(saves)]   # written once
    _cycle(plugin, _MapServer(_Mission("Kola")), saves)
    assert len([w for w in writes if w.endswith("last_map.json")]) == 2


def test_same_map_in_file_is_not_rewritten_after_restart(tmp_path, monkeypatch):
    saves, ch = _saves(tmp_path), _Channel({})
    _cycle(_plugin(tmp_path, ch), _MapServer(_Mission("Syria")), saves)
    writes = []
    real = commands.write_bytes_to_node

    async def spy(node, path, data, log=None):
        writes.append(path)
        return await real(node, path, data, log=log)
    monkeypatch.setattr(commands, "write_bytes_to_node", spy)
    _cycle(_plugin(tmp_path, ch), _MapServer(_Mission("Syria")), saves)
    assert not [w for w in writes if w.endswith("last_map.json")]


def test_new_server_without_saves_folder_stores_nothing_and_stays_quiet(tmp_path, caplog):
    ch = _Channel({})
    missing = str(tmp_path / "Saves")                          # mission never ran: no folder yet
    with caplog.at_level(logging.DEBUG):
        _cycle(_plugin(tmp_path, ch), _MapServer(_Mission("Syria")), missing)
    assert _map_line(ch) == ["🗺️  Syria"]                  # still shown, from DCSSB
    assert not os.path.exists(missing)
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_map_is_stored_once_the_folder_appears(tmp_path):
    ch = _Channel({})
    plugin = _plugin(tmp_path, ch)
    saves = str(tmp_path / "Saves")
    _cycle(plugin, _MapServer(_Mission("Syria")), saves)
    os.makedirs(saves)
    _cycle(plugin, _MapServer(_Mission("Syria")), saves)
    assert json.load(open(_map_file(saves))) == {"map": "Syria"}


def test_corrupt_or_odd_map_file_is_ignored(tmp_path):
    for content in ("not json", "[1, 2]", '{"map": 5}', '{"map": ""}'):
        saves = tmp_path / "s"
        shutil.rmtree(saves, ignore_errors=True)
        (saves / ".fhc").mkdir(parents=True)
        (saves / ".fhc" / "last_map.json").write_text(content)
        ch = _Channel({})
        _cycle(_plugin(tmp_path, ch), _MapServer(None), str(saves))
        assert _map_line(ch) == [], content


def test_maps_are_kept_per_instance(tmp_path):
    one, two = tmp_path / "one", tmp_path / "two"
    for d in (one, two):
        (d / ".fhc").mkdir(parents=True)
    other = _MapServer(_Mission("Kola"))
    other.instance = type("I", (), {"name": "other"})()
    plugin = _plugin(tmp_path, _Channel({}))
    node = _Server().node
    known = lambda srv, d: asyncio.run(plugin._known_map(srv, str(d), node))
    assert known(_MapServer(_Mission("Syria")), one) == "Syria"
    assert known(other, two) == "Kola"
    assert known(_MapServer(None), one) == "Syria"
    assert json.load(open(_map_file(str(two)))) == {"map": "Kola"}


def test_not_started_embed_also_shows_the_map(tmp_path):
    ch = _Channel({})
    _cycle(_plugin(tmp_path, ch), _MapServer(_Mission("Syria")), _saves(tmp_path, with_save=False))
    assert "Campaign not started" in ch.sent.description and _map_line(ch) == ["🗺️  Syria"]


def test_embed_without_map_is_unchanged():
    embed = commands.build_embed({"blue": [], "red": [], "neutral": 0}, {}, {"campaign_name": "C"})
    assert embed.title == "📡  C" and "🗺️" not in embed.description


def test_existing_message_with_map_in_title_is_still_adopted(tmp_path):
    saves = _saves(tmp_path)
    ch = _Channel({10: "📡  C\n🗺️  Syria"})                    # id lost: found by the first title line
    _cycle(_plugin(tmp_path, ch), _MapServer(_Mission("Syria")), saves)
    assert ("send",) not in ch.log and ("adopt_edit", 10) in ch.log


def test_overlong_title_stays_within_discords_limit():
    assert len(commands._embed_title("x" * 300, "Syria")) == 256


def _title_cfg(**extra):
    return dict({"campaign_name": "C"}, **extra)


def test_show_map_defaults_to_true_and_reads_flexible_values():
    for cfg, expected in (({}, True), ({"show_map": True}, True), ({"show_map": "true"}, True),
                          ({"show_map": 1}, True), ({"show_map": False}, False),
                          ({"show_map": "false"}, False), ({"show_map": 0}, False)):
        embed = commands.build_embed({"blue": [], "red": [], "neutral": 0}, {}, _title_cfg(**cfg),
                                     map_name="Syria")
        assert (embed.title == "📡  C\n🗺️  Syria") is expected, cfg


def test_show_map_false_hides_the_map_everywhere_but_still_tracks_it(tmp_path):
    for with_save in (True, False):                              # full report and "not started" embed
        base = tmp_path / str(with_save)
        base.mkdir()
        saves, ch = _saves(base, with_save=with_save), _Channel({})
        plugin = _plugin(base, ch)
        asyncio.run(plugin._update_server(_MapServer(_Mission("Syria")), {
            "saves_dir": saves, "channel_id": 5, "campaign_name": "C", "report_layout": "R",
            "show_map": False}))
        assert ch.sent.title == "📡  C" and _map_line(ch) == []
        assert json.load(open(_map_file(saves))) == {"map": "Syria"}      # remembered while hidden
        asyncio.run(plugin._update_server(_MapServer(None), {
            "saves_dir": saves, "channel_id": 5, "campaign_name": "C", "report_layout": "R"}))
        assert ch.sent.title == "📡  C\n🗺️  Syria"                         # appears once enabled
