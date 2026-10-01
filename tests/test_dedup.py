"""deduplicate_ranks: merging callsign-change duplicates in old-format Foothold_Ranks.lua."""
import asyncio
import os

from conftest import FileNode, commands


def test_merges_rename_keeping_career(saves_dir):
    d = saves_dir(ranks="ranks_old.lua", campaign=None)
    path = os.path.join(d, "Foothold_Ranks.lua")
    assert asyncio.run(commands.deduplicate_ranks(path, None, FileNode()))
    players = asyncio.run(commands.parse_ranks(path, [], FileNode()))
    assert "UZI 1-1 Zarpa" not in players
    assert players["Zarpa"] == {"credits": 5400.0, "ucid": "a" * 32, "career": {1: 7200.0, 10: 5.0}}
    text = open(path, encoding="utf-8").read()
    assert '["lastSeen"]=200' in text and "\n  ['Viper']={" in text


def test_new_format_is_left_alone(saves_dir):
    d = saves_dir(ranks="ranks_new.lua", campaign=None)
    path = os.path.join(d, "Foothold_Ranks.lua")
    before = open(path, encoding="utf-8").read()
    assert not asyncio.run(commands.deduplicate_ranks(path, None, FileNode()))
    assert open(path, encoding="utf-8").read() == before
