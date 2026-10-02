"""Foothold files missing or unusual: new server, campaign not started yet,
mission restart, half-written or non-UTF-8 files."""
import asyncio
import logging
import os
import shutil

import pytest

from conftest import FIXTURES, FileNode, commands, make_plugin
from test_update_server import _Bot, _Channel, _Server


@pytest.fixture(autouse=True)
def _reset_warnings():
    commands._missing_save_warned.clear()
    yield
    commands._missing_save_warned.clear()


def _cycle(plugin, channel, saves, layout="DSRP", cycles=1):
    for _ in range(cycles):
        asyncio.run(plugin._update_server(_Server(), {"saves_dir": saves, "channel_id": 5,
                                                      "campaign_name": "C", "report_layout": layout}))
    return channel.sent


def _setup(tmp_path, **files):
    saves = tmp_path / "Saves"
    for name, fixture in files.items():
        saves.mkdir(exist_ok=True)
        shutil.copy(os.path.join(FIXTURES, fixture), saves / name)
    channel = _Channel({})
    return str(saves), channel, make_plugin(bot=_Bot(channel), _message_ids_file=str(tmp_path / "ids.json"))


def _names(embed):
    return [f.name.strip() for f in embed.fields]


def test_new_server_without_saves_folder_shows_not_started(tmp_path, caplog):
    saves, channel, plugin = _setup(tmp_path)
    with caplog.at_level(logging.DEBUG):
        embed = _cycle(plugin, channel, saves, cycles=3)
    assert embed.title == "📡  C"
    assert "Campaign not started" in embed.description
    assert not any("Zones" in n for n in _names(embed))
    assert channel.log.count(("send",)) == 1                  # posted once, then edited
    notes = [r for r in caplog.records if "no foothold_*.lua" in r.message]
    assert len(notes) == 1 and notes[0].levelno == logging.DEBUG   # once, DEBUG only


def test_not_started_shows_rank_table_when_ranks_exist(tmp_path):
    saves, channel, plugin = _setup(tmp_path, **{"Foothold_Ranks.lua": "ranks_new.lua"})
    embed = _cycle(plugin, channel, saves, layout="DSRP")
    assert any("Pilot Leaderboard" in n for n in _names(embed))
    assert not any("Session" in n or "Daily" in n or "Podium" in n for n in _names(embed))
    assert "Viper" in embed.fields[0].value


def test_not_started_without_r_in_layout_has_no_table(tmp_path):
    saves, channel, plugin = _setup(tmp_path, **{"Foothold_Ranks.lua": "ranks_new.lua"})
    embed = _cycle(plugin, channel, saves, layout="DS")
    assert "Campaign not started" in embed.description
    assert len(embed.fields) == 1                             # just the closing ruler


def test_same_message_becomes_full_report_when_save_appears(tmp_path, caplog):
    saves, channel, plugin = _setup(tmp_path, **{"Foothold_Ranks.lua": "ranks_new.lua"})
    _cycle(plugin, channel, saves)
    shutil.copy(os.path.join(FIXTURES, "foothold_new.lua"), os.path.join(saves, "foothold_x.lua"))
    with caplog.at_level(logging.DEBUG):
        embed = _cycle(plugin, channel, saves)
    assert any("BLUE Zones" in n for n in _names(embed))
    assert "Campaign not started" not in embed.description
    assert channel.log == [("send",), ("partial_edit", 99)]  # no second message
    assert any("full report resumed" in r.message for r in caplog.records)


@pytest.mark.parametrize("ranks", [True, False])
def test_fresh_or_truncated_files_still_render(tmp_path, ranks):
    saves = tmp_path / "Saves"
    saves.mkdir()
    (saves / "foothold_x.lua").write_bytes(
        b'zonePersistance = {}\nzonePersistance["zones"]={}\nzonePersistance["zones"]["A"] = {["side"]=2,["lev')
    if ranks:
        (saves / "Foothold_Ranks.lua").write_bytes(b'RankSave = {}\nRankSave["players"]={\n  ["x"]={\n    ["cred')
    channel = _Channel({})
    plugin = make_plugin(bot=_Bot(channel), _message_ids_file=str(tmp_path / "ids.json"))
    embed = _cycle(plugin, channel, str(saves))
    assert any("BLUE Zones" in n for n in _names(embed))


def test_invalid_utf8_save_still_renders(tmp_path):
    saves = tmp_path / "Saves"
    saves.mkdir()
    shutil.copy(os.path.join(FIXTURES, "foothold_old.lua"), saves / "foothold_x.lua")
    with open(saves / "foothold_x.lua", "ab") as f:
        f.write(b'\n-- \xff\xfe broken bytes\n')
    channel = _Channel({})
    plugin = make_plugin(bot=_Bot(channel), _message_ids_file=str(tmp_path / "ids.json"))
    embed = _cycle(plugin, channel, str(saves))
    assert any("BLUE Zones (1)" in n for n in _names(embed))


def test_dedup_never_rewrites_undecodable_ranks(tmp_path):
    path = tmp_path / "Foothold_Ranks.lua"
    data = open(os.path.join(FIXTURES, "ranks_old.lua"), "rb").read() + b"\n-- \xff\n"
    path.write_bytes(data)
    assert not asyncio.run(commands.deduplicate_ranks(str(path), None, FileNode()))
    assert path.read_bytes() == data


def test_stale_status_file_falls_back_to_newest_save(tmp_path):
    saves = tmp_path / "Saves"
    saves.mkdir()
    shutil.copy(os.path.join(FIXTURES, "foothold_new.lua"), saves / "foothold_b.lua")
    (saves / "foothold.status").write_text("C:\\Old\\foothold_gone.lua")
    found = asyncio.run(commands.find_persistence_file(str(saves), FileNode()))
    assert found == str(saves / "foothold_b.lua")
