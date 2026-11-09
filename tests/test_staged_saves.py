"""Foothold's background (staged) save writes every table one line per entry; the files must read the same as
the ones written in one block, and the pilots Foothold keeps by name must not be lost."""
import asyncio

import pytest

from conftest import FileNode, commands

A, B = "a" * 32, "b" * 32


def run(coro):
    return asyncio.run(coro)


def _write(tmp_path, text, name="foothold_x.lua"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return str(path)


NESTED = f'''zonePersistance = {{}}
zonePersistance["playerStatsIdentityVersion"] = 1
zonePersistance["playerStats"] = {{
  ["{A}"]={{
    ["name"]="Zarpa",
    ["stats"]={{
      ["Points"]=150,
      ["Ship"]=2,
    }},
  }},
  ["{B}"]={{
    ["name"]="Viper",
    ["stats"]={{
      ["Points"]=40,
    }},
  }},
}}
'''

FLAT = f'''zonePersistance = {{}}
zonePersistance["playerStatsIdentityVersion"] = 1
zonePersistance["playerStats"] = {{}}
zonePersistance["playerStats"]["{A}"] = {{
  ["name"]="Zarpa",
  ["stats"]={{
    ["Points"]=150,
    ["Ship"]=2,
  }},
}}
zonePersistance["playerStats"]["{B}"] = {{
  ["name"]="Viper",
  ["stats"]={{
    ["Points"]=40,
  }},
}}
'''


def test_the_pilots_points_read_the_same_in_one_block_or_one_line_per_pilot(tmp_path):
    nested = run(commands.parse_player_stats(_write(tmp_path, NESTED, "n.lua"), FileNode()))
    flat = run(commands.parse_player_stats(_write(tmp_path, FLAT, "f.lua"), FileNode()))
    assert flat == nested
    assert flat[0] == {"Zarpa": 150, "Viper": 40} and flat[1]["Zarpa"] == {"Ship": 2} and flat[2] == {"Zarpa": A, "Viper": B}


def test_a_file_with_both_forms_gives_every_pilot_once(tmp_path):
    both = NESTED.replace("}\n", "}\n", 1) + f'''zonePersistance["playerStats"]["{"c" * 32}"] = {{
  ["name"]="Extra",
  ["stats"]={{
    ["Points"]=7,
  }},
}}
zonePersistance["playerStats"]["{A}"] = {{
  ["name"]="Zarpa",
  ["stats"]={{
    ["Points"]=999,
  }},
}}
'''
    stats = run(commands.parse_player_stats(_write(tmp_path, both), FileNode()))[0]
    assert stats == {"Zarpa": 150, "Viper": 40, "Extra": 7}                 # the first one found wins


def test_names_with_braces_and_quotes_survive_in_the_one_line_form(tmp_path):
    name = 'Joe :}" {x'
    text = (f'zonePersistance = {{}}\nzonePersistance["playerStatsIdentityVersion"] = 1\n'
            f'zonePersistance["playerStats"] = {{}}\nzonePersistance["playerStats"]["{A}"] = {{\n'
            f'  ["name"]={commands._lua_quote(name)},\n  ["stats"]={{\n    ["Points"]=12,\n  }},\n}}\n')
    assert run(commands.parse_player_stats(_write(tmp_path, text), FileNode()))[0] == {name: 12}


def test_pilots_kept_by_name_are_listed_without_a_ucid_and_a_ucid_record_wins(tmp_path):
    legacy = ('zonePersistance["legacyPlayerStats"] = {}\n'
              'zonePersistance["legacyPlayerStats"]["Old Pilot"] = {\n  ["Points"]=4000,\n  ["Air"]=9,\n}\n'
              'zonePersistance["legacyPlayerStats"]["Zarpa"] = {\n  ["Points"]=1,\n}\n')
    stats, raw, ucids = run(commands.parse_player_stats(_write(tmp_path, FLAT + legacy), FileNode()))
    assert stats == {"Zarpa": 150, "Viper": 40, "Old Pilot": 4000} and raw["Old Pilot"] == {"Air": 9}
    assert "Old Pilot" not in ucids and ucids["Zarpa"] == A
    nested = NESTED + ('zonePersistance["legacyPlayerStats"] = {\n  ["Old Pilot"]={\n    ["Points"]=4000,\n  },\n}\n')
    assert run(commands.parse_player_stats(_write(tmp_path, nested, "n.lua"), FileNode()))[0]["Old Pilot"] == 4000


def test_the_old_name_keyed_file_is_still_read_in_either_form(tmp_path):
    flat = ('zonePersistance = {}\nzonePersistance["playerStats"] = {}\n'
            'zonePersistance["playerStats"]["Bilbor"] = {\n  ["Points"]=230,\n  ["Ground Units"]=3,\n}\n'
            f'zonePersistance["ucidToName"] = {{}}\nzonePersistance["ucidToName"]["{A}"] = "Bilbor"\n')
    stats, raw, ucids = run(commands.parse_player_stats(_write(tmp_path, flat), FileNode()))
    assert stats == {"Bilbor": 230} and raw["Bilbor"] == {"Ground Units": 3} and ucids == {"Bilbor": A}


RANKS = f'''RankSave = {{}}
RankSave["settingsIdentityVersion"] = 1
RankSave["version"] = 2
RankSave["playerIdentityVersion"] = 2

RankSave["players"] = {{
  ["{A}"]={{
    ["name"]="Zarpa",
    ["lastSeen"]=1730000000,
    ["credits"]=5100,
    ["aircraft"]={{
      ["F-16C_50"]={{
        ["hours"]=12.5,
      }},
    }},
    ["career"]={{
      [1]=7200,
      [3]=600,
    }},
  }},
  ["{B}"]={{
    ["name"]="Viper",
    ["lastSeen"]=1730000100,
  }},
}}
RankSave["legacyPlayers"] = {{}}
RankSave["legacyPlayers"]["Old Pilot"] = {{
  ["name"]="Old Pilot",
  ["lastSeen"]=5,
  ["credits"]=9000,
  ["career"]={{
    [10]=33,
  }},
}}
RankSave["legacyPlayers"]["HORNET 1-1 | Zarpa"] = {{
  ["credits"]=70,
}}
RankSave["playerAliases"] = {{}}
RankSave["ewrs"] = {{}}
'''


def test_the_rank_file_in_the_new_layout_is_read_with_the_pilots_kept_by_name(tmp_path):
    ranks = run(commands.parse_ranks(_write(tmp_path, RANKS, "Foothold_Ranks.lua"), [], FileNode()))
    assert list(ranks) == ["Old Pilot", "Zarpa"]                                   # best first; Viper has no credits
    assert ranks["Zarpa"] == {"credits": 5100.0, "ucid": A, "career": {1: 7200.0, 3: 600.0}}
    assert ranks["Old Pilot"] == {"credits": 9000.0, "ucid": None, "career": {10: 33.0}}   # no UCID yet
    # a record kept by name for a pilot who already has one (same name once the callsign is left out) is not a second pilot
    assert "HORNET 1-1 | Zarpa" not in ranks


def test_a_rank_file_with_the_pilots_kept_by_name_in_one_block_reads_too(tmp_path):
    nested = RANKS.split('RankSave["legacyPlayers"]')[0] + (
        'RankSave["legacyPlayers"] = {\n  ["Old Pilot"]={\n    ["credits"]=9000,\n  },\n}\n')
    assert run(commands.parse_ranks(_write(tmp_path, nested, "r.lua"), [], FileNode()))["Old Pilot"]["credits"] == 9000.0


def test_the_pilots_of_the_rank_file_are_known_by_name_even_without_credits(tmp_path):
    identities = run(commands.read_rank_identities(_write(tmp_path, RANKS, "Foothold_Ranks.lua"), FileNode()))
    assert identities == {"Zarpa": A, "Viper": B}


def test_a_pilot_known_only_by_identity_is_listed_as_unranked_with_the_ucid_of_the_rank_file():
    players = {}
    commands.add_unranked_pilots(players, {"Viper": 40}, {}, {}, {"Viper": B})
    assert players["Viper"]["ucid"] == B and players["Viper"]["unranked"]


class _MissingNode:
    def __init__(self, files):
        self.files, self.reads = files, []

    async def read_file(self, path):
        self.reads.append(path)
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]


def test_a_file_caught_between_foothold_renames_is_read_from_its_backup():
    node = _MissingNode({"/s/Foothold_Ranks.lua.staged-backup": b"old"})
    cache = commands._UpdateReadCache(node)
    assert run(cache.read_file("/s/Foothold_Ranks.lua")) == b"old"
    run(cache.read_file("/s/Foothold_Ranks.lua"))
    assert node.reads == ["/s/Foothold_Ranks.lua", "/s/Foothold_Ranks.lua.staged-backup"]       # and it is cached


def test_the_backup_is_only_for_the_lua_files_and_a_missing_file_is_still_missing():
    cache = commands._UpdateReadCache(_MissingNode({"/s/x.json.staged-backup": b"old"}))
    with pytest.raises(FileNotFoundError):
        run(cache.read_file("/s/x.json"))
    with pytest.raises(FileNotFoundError):
        run(commands._UpdateReadCache(_MissingNode({})).read_file("/s/Foothold_Ranks.lua"))
    real = commands._UpdateReadCache(_MissingNode({"/s/Foothold_Ranks.lua": b"new", "/s/Foothold_Ranks.lua.staged-backup": b"old"}))
    assert run(real.read_file("/s/Foothold_Ranks.lua")) == b"new"                                # the file wins
