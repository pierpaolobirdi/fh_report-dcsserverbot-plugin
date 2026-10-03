"""Foothold file parsing, both on-disk formats (old name-keyed and 4.9.1+ UCID-keyed)."""
import asyncio
import os

from conftest import FIXTURES, FileNode, commands

A, B, C = "a" * 32, "b" * 32, "c" * 32


def run(coro):
    return asyncio.run(coro)


def fx(name):
    return os.path.join(FIXTURES, name)


def test_parse_ranks_old_format():
    players = run(commands.parse_ranks(fx("ranks_old.lua"), [], FileNode()))
    assert list(players) == ["Viper", "UZI 1-1 Zarpa", "Excl", "Zarpa"]   # sorted by credits
    assert players["UZI 1-1 Zarpa"] == {"credits": 5100.0, "ucid": A, "career": {1: 7200.0, 10: 5.0}}
    assert players["Viper"]["ucid"] == B          # single-quoted ucidToName entry
    assert players["Zarpa"]["ucid"] is None       # orphan left by a rename


def test_parse_ranks_new_format():
    players = run(commands.parse_ranks(fx("ranks_new.lua"), [], FileNode()))
    assert list(players) == ["Viper", "Zarpa", "Excl"]
    assert players["Zarpa"] == {"credits": 5100.0, "ucid": A, "career": {1: 7200.0, 3: 600.0}}


def test_excluded_ucids_both_formats():
    for f in ("ranks_old.lua", "ranks_new.lua"):
        players = run(commands.parse_ranks(fx(f), [C], FileNode()))
        assert "Excl" not in players, f


def test_parse_player_stats_old_format():
    pts, stats, n2u = run(commands.parse_player_stats(fx("foothold_old.lua"), FileNode()))
    assert pts == {"Zarpa": 150, "Viper": 20}
    assert stats == {"Zarpa": {"Air": 3, "CAP mission": 1.5}, "Viper": {}}
    assert n2u == {"Viper": B}                    # from the optional ucidToName table


def test_parse_player_stats_new_format():
    pts, stats, n2u = run(commands.parse_player_stats(fx("foothold_new.lua"), FileNode()))
    assert pts == {"Zarpa": 150, "Viper": 40}
    assert stats == {"Zarpa": {"Ship": 2}, "Viper": {}}
    assert n2u == {"Zarpa": A, "Viper": B}


def test_parse_zones():
    zones = run(commands.parse_zones(fx("foothold_old.lua"), FileNode()))
    assert zones["neutral"] == 1                  # hiddenX is skipped
    assert zones["blue"] == [{"name": "Alpha", "level": 3, "active_slots": 2,
                              "suspended": False, "true_max": 6}]
    assert zones["red"] == [{"name": "Bravo", "level": 2, "active_slots": 1, "suspended": True}]


def test_unknown_format_version_is_skipped(tmp_path):
    f = tmp_path / "Foothold_Ranks.lua"
    f.write_text('RankSave = {}\nRankSave["playerIdentityVersion"]=9\nRankSave["players"]={}\n')
    assert run(commands.parse_ranks(str(f), [], FileNode())) == {}


def test_strip_callsign():
    cases = {
        "UZI 1-1 zarpa": "zarpa",
        "UZI 1_4 Silver": "Silver",
        "GUNSTAR 11 | DRCHOW | 307": "DRCHOW",
        "[MA] Leka": "[MA] Leka",
        "132nd Kimkiller": "132nd Kimkiller",
        "Alpha/Bravo": "Bravo",
    }
    for raw, expected in cases.items():
        assert commands.strip_callsign(raw) == expected, raw


def test_session_card_bnb_priority_and_cap():
    from conftest import commands
    stats = {"Air": 3, "SAM": 1, "Ground Units": 4, "Ship": 1, "Pilot Rescue": 1,
             "Refueling": 2, "Deaths": 2, "Achievement": 1, "Mission X": 1}
    card = commands._build_session_card(stats, bnb=2)
    parts = card.split(" · ")
    assert len(parts) == 7 and parts[-2] == "B&B: 2" and parts[-1] == "2 Deaths"
    assert commands._build_session_card({}, bnb=1).endswith("B&B: 1")
    assert commands._build_session_card({}, bnb=0) is None
    assert "B&B" not in commands._build_session_card({"Air": 1})


def test_pilot_card_bnb_is_penultimate():
    from conftest import commands
    card = commands._build_pilot_card({10: 47, 21: 3}, bnb=2)
    assert card.endswith("47 Kills · B&B: 2 · 3 Deaths")
    assert "B&B" not in commands._build_pilot_card({10: 47, 21: 3})
    assert commands._build_pilot_card({}, bnb=1).endswith("B&B: 1")


def test_day_start_uses_schedule():
    from datetime import datetime, timezone
    from conftest import commands
    cfg = {"daily_reset_hour": 2, "daily_reset_schedule": {"sat": 6}}
    fri = datetime(2026, 10, 2, 1, 0, tzinfo=timezone.utc)   # before today's reset
    assert commands._day_start(cfg, fri) == datetime(2026, 10, 1, 2, tzinfo=timezone.utc)
    sat = datetime(2026, 10, 3, 5, 0, tzinfo=timezone.utc)   # Saturday resets at 6
    assert commands._day_start(cfg, sat) == datetime(2026, 10, 2, 2, tzinfo=timezone.utc)
    assert commands._day_start(cfg, sat.replace(hour=7)) == datetime(2026, 10, 3, 6, tzinfo=timezone.utc)
