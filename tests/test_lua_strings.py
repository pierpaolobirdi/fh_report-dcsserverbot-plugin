"""Foothold writes every string with string.format('%q'): player names, stat keys and zone
names can contain apostrophes, double quotes, backslashes, braces and even newlines."""
import asyncio
import random

import pytest

from conftest import FileNode, commands

TRICKY_NAMES = [
    "Normal", "O'Brien", 'Viper "The" Jones', "Back\\slash", "[MA] Leka", "Zoë | UZI 1-1",
    "Joe :}", "{Joe", "Pilot {RAF}", "a]=b", '["credits"]=9', "it's \"both\"", "tab\there",
    "two\nlines", "end\\", "'", '"', "x'] = 5 ['y",
]


def q(text):                                              # exactly what Lua's %q writes
    return commands._lua_quote(text)


def ucid(i):
    return f"{i:032x}"


def run(coro):
    return asyncio.run(coro)


def write(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return str(path)


# ── the building blocks ───────────────────────────────────────────────────────

def test_quote_and_unquote_round_trip_every_tricky_name():
    for name in TRICKY_NAMES + ["", "\r\n", "\x00", "päß"]:
        assert commands._lua_unquote(commands._lua_quote(name)) == name


def test_random_strings_round_trip():
    rnd = random.Random(1)
    alphabet = ["a", "b", "'", '"', "\\", "{", "}", "[", "]", "=", "\n", " ", "é"]
    for _ in range(300):
        text = "".join(rnd.choice(alphabet) for _ in range(rnd.randint(0, 12)))
        assert commands._lua_unquote(commands._lua_quote(text)) == text


def test_unquote_understands_lua_escapes_and_both_quote_styles():
    assert commands._lua_unquote(r'"a\"b\\c\nd"') == 'a"b\\c\nd'
    assert commands._lua_unquote("'it\\'s'") == "it's"
    assert commands._lua_unquote('"a\\\nb"') == "a\nb"            # backslash + newline
    assert commands._lua_unquote(r'"\065\000"') == "A\x00"


def test_braces_inside_strings_do_not_end_a_block():
    text = '{ ["name"]="Joe :}", ["x"]={ ["y"]="{" }, ["z"]=1 } tail'
    inner, end = commands._lua_block(text, 0)
    assert inner == ' ["name"]="Joe :}", ["x"]={ ["y"]="{" }, ["z"]=1 ' and text[end:] == " tail"


def test_unbalanced_block_returns_the_rest():
    inner, end = commands._lua_block('{ ["a"]={ ["b"]=1', 0)
    assert end == len('{ ["a"]={ ["b"]=1') and inner.startswith(' ["a"]')


def test_entries_skip_nested_tables_and_decode_keys():
    block = '["O\'Brien"]={ ["career"]={ [1]=5 }, ["credits"]=1 }, [\'Old\']={ ["credits"]=2 }'
    assert [k for k, _ in commands._lua_entries(block)] == ["O'Brien", "Old"]


# ── Ranks and playerStats with those names ────────────────────────────────────

def ranks_file(tmp_path, names, version=2):
    body = "".join(f'  [{q(ucid(i))}]={{\n    ["name"]={q(n)},\n    ["lastSeen"]=1,\n    ["credits"]={100 * i},\n'
                   f'    ["career"]={{ [1]=60, [10]={i} }},\n  }},\n' for i, n in enumerate(names, 1))
    return write(tmp_path, "Foothold_Ranks.lua",
                 f'RankSave = {{}}\nRankSave["playerIdentityVersion"] = {version}\nRankSave["players"] = {{\n{body}}}\n')


def test_every_tricky_name_survives_parse_ranks(tmp_path):
    players = run(commands.parse_ranks(ranks_file(tmp_path, TRICKY_NAMES), [], FileNode()))
    stripped = {n.strip() for n in TRICKY_NAMES if len(n.strip()) >= 2}
    assert set(players) == stripped
    for i, name in enumerate(TRICKY_NAMES, 1):
        if name in players:
            assert players[name]["ucid"] == ucid(i) and players[name]["credits"] == 100.0 * i
            assert players[name]["career"] == {1: 60.0, 10: float(i)}


def test_every_tricky_name_survives_parse_player_stats(tmp_path):
    body = "".join(f'  [{q(ucid(i))}]={{\n    ["name"]={q(n)},\n    ["stats"]={{\n      ["Points"]={i},\n'
                   f'      ["Air"]=1,\n      ["Capture Cap d\'Antifer"]=2,\n      [{q(n)}]=3,\n    }},\n  }},\n'
                   for i, n in enumerate(TRICKY_NAMES, 1))
    path = write(tmp_path, "foothold_x.lua", 'zonePersistance = {}\nzonePersistance["playerStatsIdentityVersion"] = 1\n'
                                              f'zonePersistance["playerStats"] = {{\n{body}}}\n')
    points, stats, name_to_ucid = run(commands.parse_player_stats(path, FileNode()))
    assert set(points) == set(TRICKY_NAMES)
    for i, name in enumerate(TRICKY_NAMES, 1):
        assert points[name] == i and name_to_ucid[name] == ucid(i)
        assert stats[name] == {"Air": 1, "Capture Cap d'Antifer": 2, name: 3}   # keys with apostrophes kept


def test_old_name_keyed_formats_with_escapes(tmp_path):
    names = ["O'Brien", 'Say "Hi"', "Back\\slash", "Joe :}"]
    body = "".join(f"  [{q(n)}]={{\n    [\"credits\"]={100 * i},\n  }},\n" for i, n in enumerate(names, 1))
    names_map = "".join(f"  [{q(ucid(i))}]={q(n)},\n" for i, n in enumerate(names, 1))
    path = write(tmp_path, "Foothold_Ranks.lua",
                 f'RankSave = {{}}\nRankSave["players"] = {{\n{body}}}\nRankSave["ucidToName"] = {{\n{names_map}}}\n')
    players = run(commands.parse_ranks(path, [ucid(4)], FileNode()))
    assert sorted(players) == sorted(names[:3])                    # the excluded UCID's name is gone
    assert players["O'Brien"]["ucid"] == ucid(1) and players['Say "Hi"']["credits"] == 200.0


def test_zone_names_with_apostrophes_and_quotes(tmp_path):
    def zone(name, side):
        body = '{["side"]=%d,["active"]=true,["level"]=2,["remainingUnits"]={[1]={"u"},[2]={}}}' % side
        return 'zonePersistance["zones"][' + q(name) + "] = " + body + "\n"
    path = write(tmp_path, "foothold_x.lua", 'zonePersistance = {}\nzonePersistance["zones"] = {}\n'
                 + zone("Cap d'Antibes", 2) + zone('"Quoted"', 1))
    zones = run(commands.parse_zones(path, FileNode()))
    assert [z["name"] for z in zones["blue"]] == ["Cap d'Antibes"]
    assert [z["name"] for z in zones["red"]] == ['"Quoted"']


# ── rewriting an old-format Ranks file must keep it valid Lua ─────────────────

@pytest.mark.parametrize("base", ["O'Brien", 'Say "Hi"', "Back\\slash", "Joe :}"])
def test_dedup_writes_names_the_way_foothold_does(tmp_path, base):
    old_name = f"UZI 1-1 {base}"
    body = (f"  [{q(old_name)}]={{\n    [\"credits\"]=300,\n    [\"lastSeen\"]=1,\n    [\"career\"]={{ [1]=60 }},\n  }},\n"
            f"  [{q(base)}]={{\n    [\"credits\"]=50,\n    [\"lastSeen\"]=2,\n  }},\n")
    path = write(tmp_path, "Foothold_Ranks.lua",
                 f'RankSave = {{}}\nRankSave["players"] = {{\n{body}}}\n'
                 f'RankSave["ucidToName"] = {{\n  [{q(ucid(1))}]={q(old_name)},\n}}\n')
    assert run(commands.deduplicate_ranks(path, None, FileNode()))
    canonical = commands.strip_callsign(old_name)                     # a backslash is a callsign separator
    text = open(path, encoding="utf-8").read()
    assert f"[{q(canonical)}]={{" in text and f"[{q(ucid(1))}]={q(canonical)}," in text
    players = run(commands.parse_ranks(path, [], FileNode()))
    assert list(players) == [canonical]
    assert players[canonical] == {"credits": 350.0, "ucid": ucid(1), "career": {1: 60.0}}
