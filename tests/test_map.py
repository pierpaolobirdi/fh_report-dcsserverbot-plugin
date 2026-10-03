"""Map on the second line of the title: taken from DCSServerBot (the running mission),
kept in memory, replaced when DCSSB reports another map, read once from the mission file
when nothing is known, and nothing shown when it never is. No file is written for it."""
import asyncio
import os
import shutil

from conftest import FIXTURES, commands, make_plugin
from test_update_server import _Bot, _Channel, _Server


class _Mission:
    def __init__(self, map_name):
        self.map = map_name


class _MapServer(_Server):
    """A DCSSB server: `mission` is what current_mission holds; `theatre` is what
    get_current_mission_theatre() would read from the .miz (a call is recorded)."""
    def __init__(self, mission=None, theatre=None, name="inst"):
        super().__init__()
        self.current_mission, self.theatre, self.theatre_calls = mission, theatre, 0
        self.instance = type("I", (), {"name": name})()

    async def get_current_mission_theatre(self):
        self.theatre_calls += 1
        if isinstance(self.theatre, Exception):
            raise self.theatre
        if self.theatre == "hang":
            await asyncio.sleep(3600)
        return self.theatre


class _BrokenMissionServer(_MapServer):
    @property
    def current_mission(self):
        raise RuntimeError("mission not ready")

    @current_mission.setter
    def current_mission(self, _):
        pass


def _plugin(tmp_path, channel):
    return make_plugin(bot=_Bot(channel), _message_ids_file=str(tmp_path / "ids.json"))


def _cycle(plugin, server, saves, layout="R", **cfg):
    asyncio.run(plugin._update_server(server, dict({"saves_dir": saves, "channel_id": 5, "campaign_name": "C",
                                                    "report_layout": layout}, **cfg)))


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


def _map_files(saves):
    fhc = os.path.join(saves, ".fhc")
    return [f for f in os.listdir(fhc) if "map" in f] if os.path.isdir(fhc) else []


def test_map_is_the_second_line_of_the_title(tmp_path):
    ch = _Channel({})
    _cycle(_plugin(tmp_path, ch), _MapServer(_Mission("Syria")), _saves(tmp_path))
    assert ch.sent.title == "📡  C\n🗺️  Syria"
    assert ch.sent.description.startswith("**Front Status — ") and "🗺️" not in ch.sent.description


def test_running_mission_never_touches_the_mission_file(tmp_path):
    server = _MapServer(_Mission("Syria"), theatre="Caucasus")
    _cycle(_plugin(tmp_path, _Channel({})), server, _saves(tmp_path))
    assert server.theatre_calls == 0


def test_no_file_is_written_for_the_map(tmp_path):
    saves = _saves(tmp_path)
    _cycle(_plugin(tmp_path, _Channel({})), _MapServer(_Mission("Syria")), saves, layout="D")
    assert _map_files(saves) == []


def test_unknown_map_shows_nothing(tmp_path):
    for server in (_MapServer(None), _MapServer(_Mission("")), _MapServer(_Mission(None)),
                   _BrokenMissionServer(), _MapServer(None, theatre=None), _MapServer(None, theatre="  "),
                   _MapServer(None, theatre=RuntimeError("bad miz"))):
        ch = _Channel({})
        base = tmp_path / str(id(server))
        base.mkdir()
        _cycle(_plugin(base, ch), server, _saves(base))
        assert _map_line(ch) == [] and ch.sent.title == "📡  C"


def test_last_known_map_is_kept_when_no_mission_is_loaded(tmp_path):
    saves, ch = _saves(tmp_path), _Channel({})
    plugin = _plugin(tmp_path, ch)
    server = _MapServer(_Mission("Syria"), theatre="Caucasus")
    _cycle(plugin, server, saves)
    server.current_mission = None                               # server stopped / mission unloading
    _cycle(plugin, server, saves)
    assert _map_line(ch) == ["🗺️  Syria"] and server.theatre_calls == 0


def test_map_is_replaced_when_dcssb_reports_another(tmp_path):
    saves, ch = _saves(tmp_path), _Channel({})
    plugin = _plugin(tmp_path, ch)
    server = _MapServer(_Mission("Syria"))
    _cycle(plugin, server, saves)
    server.current_mission = _Mission("Caucasus")
    _cycle(plugin, server, saves)
    assert _map_line(ch) == ["🗺️  Caucasus"]


def test_unknown_map_is_read_once_from_the_mission_file(tmp_path):
    saves, ch = _saves(tmp_path), _Channel({})
    plugin = _plugin(tmp_path, ch)
    server = _MapServer(None, theatre="Afghanistan")            # bot restarted, server stopped
    for _ in range(4):
        _cycle(plugin, server, saves)
    assert _map_line(ch) == ["🗺️  Afghanistan"] and server.theatre_calls == 1
    server.current_mission = _Mission("Syria")                  # DCSSB is the authority afterwards
    _cycle(plugin, server, saves)
    assert _map_line(ch) == ["🗺️  Syria"]


def test_failed_or_slow_mission_file_read_is_not_retried_every_cycle(tmp_path, monkeypatch):
    monkeypatch.setattr(commands, "MAP_PROBE_TIMEOUT", 0.05)
    for theatre in (RuntimeError("bad miz"), "hang", None):
        base = tmp_path / str(id(theatre))
        base.mkdir()
        saves, ch = _saves(base), _Channel({})
        plugin = _plugin(base, ch)
        server = _MapServer(None, theatre=theatre)
        for _ in range(3):
            _cycle(plugin, server, saves)
        assert server.theatre_calls == 1 and _map_line(ch) == [] and ch.sent is not None
        server.current_mission = _Mission("Syria")              # a started mission still fills it in
        _cycle(plugin, server, saves)
        assert _map_line(ch) == ["🗺️  Syria"]


def test_maps_are_kept_per_instance(tmp_path):
    plugin = _plugin(tmp_path, _Channel({}))
    known = lambda srv: asyncio.run(plugin._known_map(srv))
    assert known(_MapServer(_Mission("Syria"), name="one")) == "Syria"
    assert known(_MapServer(_Mission("Kola"), name="two")) == "Kola"
    assert known(_MapServer(None, name="one")) == "Syria"


def test_not_started_embed_also_shows_the_map(tmp_path):
    ch = _Channel({})
    _cycle(_plugin(tmp_path, ch), _MapServer(_Mission("Syria")), _saves(tmp_path, with_save=False))
    assert "Campaign not started" in ch.sent.description and _map_line(ch) == ["🗺️  Syria"]


def _title_cfg(**extra):
    return dict({"campaign_name": "C"}, **extra)


def test_show_map_defaults_to_true_and_reads_flexible_values():
    for cfg, expected in (({}, True), ({"show_map": True}, True), ({"show_map": "true"}, True),
                          ({"show_map": 1}, True), ({"show_map": False}, False),
                          ({"show_map": "false"}, False), ({"show_map": 0}, False)):
        embed = commands.build_embed({"blue": [], "red": [], "neutral": 0}, {}, _title_cfg(**cfg),
                                     map_name="Syria")
        assert (embed.title == "📡  C\n🗺️  Syria") is expected, cfg


def test_show_map_false_hides_the_map_everywhere(tmp_path):
    for with_save in (True, False):                              # full report and "not started" embed
        base = tmp_path / str(with_save)
        base.mkdir()
        saves, ch = _saves(base, with_save=with_save), _Channel({})
        plugin = _plugin(base, ch)
        server = _MapServer(_Mission("Syria"))
        _cycle(plugin, server, saves, show_map=False)
        assert ch.sent.title == "📡  C" and _map_line(ch) == []
        _cycle(plugin, server, saves)                                   # enabled again: shown at once
        assert ch.sent.title == "📡  C\n🗺️  Syria"


def test_show_map_false_does_not_read_the_mission_file(tmp_path):
    server = _MapServer(None, theatre="Syria")
    _cycle(_plugin(tmp_path, _Channel({})), server, _saves(tmp_path), show_map=False)
    assert server.theatre_calls == 0


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
