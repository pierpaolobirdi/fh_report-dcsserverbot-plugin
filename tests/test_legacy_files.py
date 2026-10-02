"""The fhr_ prefix on files in saves_dir/.fhc, and living with the old names:
read the old file until the fhr_ one exists, delete the old one only on a LATER
read of a good fhr_ file, and only from the updater."""
import asyncio
import json
import os
import shutil

from conftest import FIXTURES, FileNode, commands, make_plugin
from test_update_server import _Bot, _Channel, _Server

OLD = {"date": "2026-09-01", "snapshot": {"x": 1}}
NEW = {"date": "2026-09-02", "snapshot": {"x": 2}}


def _fhc(tmp_path):
    d = tmp_path / "Saves" / ".fhc"
    d.mkdir(parents=True)
    return d


def _read(node, saves, name, cleanup=False):
    return asyncio.run(commands._read_fhr_json(node, saves, name, cleanup))


def test_fhr_prefix_names():
    assert commands._fhr_path("/s", "daily_snapshot.json") == os.path.join("/s", ".fhc", "fhr_daily_snapshot.json")
    plugin = make_plugin()
    assert plugin._get_daily_file("/s").endswith("fhr_daily_snapshot.json")
    assert plugin._get_history_file("/s").endswith("fhr_daily_history.json")
    assert plugin._get_map_file("/s").endswith("fhr_last_map.json")


def test_shared_waypoint_file_keeps_its_fhc_name(tmp_path):
    fhc = _fhc(tmp_path)
    (fhc / "fhc_waypoints.lua").write_text('WaypointList = {\n  ["Alpha"] = "3",\n}\n')
    assert asyncio.run(commands.load_waypoint_list(str(tmp_path / "Saves"), FileNode())) == {"Alpha": 3}


def test_old_file_is_read_until_the_new_one_exists(tmp_path):
    fhc, saves = _fhc(tmp_path), str(tmp_path / "Saves")
    (fhc / "daily_snapshot.json").write_text(json.dumps(OLD))
    assert _read(FileNode(), saves, "daily_snapshot.json", cleanup=True) == (OLD, False)
    assert (fhc / "daily_snapshot.json").exists()           # reading the old file never deletes it


def test_new_file_wins_and_old_is_deleted_only_on_cleanup(tmp_path):
    fhc, saves = _fhc(tmp_path), str(tmp_path / "Saves")
    (fhc / "daily_snapshot.json").write_text(json.dumps(OLD))
    (fhc / "fhr_daily_snapshot.json").write_text(json.dumps(NEW))
    assert _read(FileNode(), saves, "daily_snapshot.json", cleanup=False) == (NEW, True)
    assert (fhc / "daily_snapshot.json").exists()           # e.g. /fh_report player: read-only
    assert _read(FileNode(), saves, "daily_snapshot.json", cleanup=True) == (NEW, True)
    assert not (fhc / "daily_snapshot.json").exists()       # updater, new file read back OK


def test_bad_new_file_falls_back_and_keeps_the_old_one(tmp_path):
    fhc, saves = _fhc(tmp_path), str(tmp_path / "Saves")
    (fhc / "daily_snapshot.json").write_text(json.dumps(OLD))
    for bad in ("{not json", "", "[]"):
        (fhc / "fhr_daily_snapshot.json").write_text(bad)
        assert _read(FileNode(), saves, "daily_snapshot.json", cleanup=True) == (OLD, False)
        assert (fhc / "daily_snapshot.json").exists()


def test_nothing_anywhere_is_empty(tmp_path):
    assert _read(FileNode(), str(tmp_path / "Saves"), "daily_history.json", cleanup=True) == ({}, False)


class _RemoteNode:
    """A node whose files only exist on the other machine."""
    def __init__(self, files):
        self.files, self.reads = files, []

    async def read_file(self, path):
        self.reads.append(path)
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]


def test_remote_node_old_file_is_left_alone_and_nothing_is_created_here(tmp_path):
    saves = "/remote-agent/Saves"
    old, new = f"{saves}/.fhc/daily_snapshot.json", f"{saves}/.fhc/fhr_daily_snapshot.json"
    node = _RemoteNode({old: json.dumps(OLD).encode()})
    assert _read(node, saves, "daily_snapshot.json", cleanup=True) == (OLD, False)
    node.files[new] = json.dumps(NEW).encode()
    assert _read(node, saves, "daily_snapshot.json", cleanup=True) == (NEW, True)
    assert old in node.files and not os.path.exists("/remote-agent")


# ── end to end through the updater ────────────────────────────────────────────

def _cycle(plugin, saves, layout="D"):
    asyncio.run(plugin._update_server(_Server(), {"saves_dir": saves, "channel_id": 5,
                                                  "campaign_name": "C", "report_layout": layout}))


def _updater_plugin(tmp_path):
    return make_plugin(bot=_Bot(_Channel({})), _message_ids_file=str(tmp_path / "ids.json"))


def _saves_with_legacy_snapshot(tmp_path):
    """A saves folder as an older version left it: daily state under the old name."""
    saves = tmp_path / "Saves"
    saves.mkdir()
    shutil.copy(os.path.join(FIXTURES, "ranks_new.lua"), saves / "Foothold_Ranks.lua")
    shutil.copy(os.path.join(FIXTURES, "foothold_new.lua"), saves / "foothold_x.lua")
    _cycle(_updater_plugin(tmp_path), str(saves))
    (saves / ".fhc" / "fhr_daily_snapshot.json").rename(saves / ".fhc" / "daily_snapshot.json")
    return saves


def test_updater_migrates_snapshot_and_deletes_old_on_a_later_cycle(tmp_path):
    saves = _saves_with_legacy_snapshot(tmp_path)
    fhc, plugin = saves / ".fhc", _updater_plugin(tmp_path)
    legacy = json.loads((fhc / "daily_snapshot.json").read_text())
    (saves / "foothold_x.lua").write_text(
        (saves / "foothold_x.lua").read_text().replace('["Points"]=150', '["Points"]=250'))

    _cycle(plugin, str(saves))                               # first cycle: old read, new written
    assert (fhc / "fhr_daily_snapshot.json").exists() and (fhc / "daily_snapshot.json").exists()
    migrated = json.loads((fhc / "fhr_daily_snapshot.json").read_text())
    assert migrated["date"] == legacy["date"]                # baseline carried over, not restarted
    assert migrated["players"] != legacy["players"]          # and today's progress was recorded

    _cycle(plugin, str(saves))                               # later cycle: new read back OK
    assert not (fhc / "daily_snapshot.json").exists()
    assert (fhc / "fhr_daily_snapshot.json").exists()


def test_player_command_never_deletes_the_old_file(tmp_path):
    saves = _saves_with_legacy_snapshot(tmp_path)
    fhc = saves / ".fhc"
    (fhc / "fhr_daily_snapshot.json").write_text((fhc / "daily_snapshot.json").read_text())
    plugin = _updater_plugin(tmp_path)
    asyncio.run(plugin._load_daily_snapshot(str(saves), FileNode()))             # what /fh_report player does
    assert (fhc / "daily_snapshot.json").exists()


def test_map_old_file_is_migrated_and_removed_on_the_next_start(tmp_path):
    saves = tmp_path / "Saves"
    fhc = _fhc(tmp_path)
    shutil.copy(os.path.join(FIXTURES, "ranks_new.lua"), saves / "Foothold_Ranks.lua")
    shutil.copy(os.path.join(FIXTURES, "foothold_new.lua"), saves / "foothold_x.lua")
    (fhc / "last_map.json").write_text('{"map": "Syria"}')

    _cycle(_updater_plugin(tmp_path), str(saves), "R")       # DCSSB doesn't know the map yet
    assert json.loads((fhc / "fhr_last_map.json").read_text()) == {"map": "Syria"}
    assert (fhc / "last_map.json").exists()                  # kept: the new file isn't confirmed yet

    restarted = _updater_plugin(tmp_path)
    _cycle(restarted, str(saves), "R")
    assert not (fhc / "last_map.json").exists()
    assert restarted._last_maps == {"inst": "Syria"}
