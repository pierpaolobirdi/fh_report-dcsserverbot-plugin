"""Rendering: every option combination must keep producing exactly the same
embed (golden file). Covers zones, all report_layout tables, Podium, cards,
punishment badges, field chunking and the /fh_report player embed."""
import copy
import itertools
import random
from datetime import datetime, timezone

import pytest

from conftest import UCID, check_golden, commands, digest, embed_dump


@pytest.fixture(autouse=True)
def _pin_release(monkeypatch):
    """The footer's length decides how many lines fit in Discord's 6000 characters, so a
    longer version number (14.1.9 -> 14.1.10) would shift the golden output."""
    monkeypatch.setattr(commands, "FH_REPORT_RELEASE", "12.5.4")


def _zone(name, level, active, suspended=False, true_max=None):
    z = {"name": name, "level": level, "active_slots": active, "suspended": suspended}
    if true_max:
        z["true_max"] = true_max
    return z


ZONES = {"blue": [_zone("Alpha", 3, 2, true_max=5), _zone("Bravo", 5, 5), _zone("Sus", 2, 0, True),
                  _zone("Delta", 7, 4, true_max=9)],
         "red": [_zone("R1", 2, 1), _zone("R2", 4, 4), _zone("R3", 1, 0, True)], "neutral": 1}
NO_ZONES = {"blue": [], "red": [], "neutral": 0}
SMALL_PLAYERS = {
    "UZI 1-1 Zarpa": {"credits": 5100.0, "ucid": UCID[1], "career": {1: 7200, 3: 600, 10: 5, 30: 123456}},
    "Viper": {"credits": 31000.0, "ucid": UCID[2], "career": {}},
    "Low": {"credits": 10.0, "ucid": None, "career": {}},
}
SMALL_DATA = dict(
    campaign_stats={"Zarpa": 150, "Viper": 0, "Low": 3},
    session_stats_raw={"Zarpa": {"Air": 2, "CAP mission": 1}, "Viper": {"SAM": 1}},
    daily_points={"Viper": 40, "UZI 1-1 Zarpa": 20},
    daily_stats_raw={"Zarpa": {"Air": 1}},
    daily_history={"2026-09-29": [{"top": [{"name": "Viper", "points": 40}, {"name": "Zarpa", "points": 10}]}],
                   "2026-09-30": [{"campaign_restart": True,
                                   "top": [{"name": "Zarpa", "points": 5, "ucid": UCID[1]}]}]},
    waypoint_map={"Alpha": 3, "Delta": 1, "R2": 2},
    points_detail={"R": "RSD"},
)


def _big_data():
    rnd = random.Random(3)
    names = [f"Pilot_{i}_{'x' * (i % 17)}" for i in range(90)]
    players = {f"UZI {i % 9}-1 {n}": {"credits": float(rnd.randint(0, 600000)), "ucid": UCID[i],
                                     "career": {1: rnd.randint(0, 99999), 10: i}}
               for i, n in enumerate(names)}
    data = dict(
        campaign_stats={n: rnd.randint(0, 900) for n in names},
        session_stats_raw={n: {"Air": rnd.randint(0, 9), f"Map mission {rnd.randint(0, 5)}": 1, "Deaths": 1}
                           for n in names},
        daily_points={k: rnd.randint(0, 300) for k in list(players)[:60]},
        daily_history={f"2026-08-{d:02d}": [{"top": [{"name": rnd.choice(names), "points": rnd.randint(1, 999)}
                                                     for _ in range(50)]}] for d in range(1, 29)},
        points_detail={"R": "RSD", "S": "S", "D": "DR"},
        punishment_points={UCID[i]: i for i in range(90)},
    )
    return players, data


def _render(players, zones, cfg, data):
    return embed_dump(commands.build_embed(copy.deepcopy(zones), copy.deepcopy(players), cfg,
                                           **copy.deepcopy(data)))


def test_embed_golden():
    results = {}
    for zones_name, slot, wp, layout, emoji, max_zones in itertools.product(
            ["zones", "empty"], [False, True], [False, True], ["R", "DSR", "P", "RP", "none"],
            [False, True], [None, 2]):
        cfg = {"campaign_name": "C", "max_zones": max_zones, "max_pilots": 2, "slot_status": slot,
               "show_pilot_card": True, "show_session_card": True, "show_daily_card": True,
               "sort_zones_by_waypoint": wp, "bar_style_emoji": emoji, "strip_callsign": True,
               "player_cmd_hint_text": "hint"}
        key = f"small|{zones_name}|slot={slot}|wp={wp}|{layout}|emoji={emoji}|maxz={max_zones}"
        results[key] = digest(_render(SMALL_PLAYERS, ZONES if zones_name == "zones" else NO_ZONES, cfg,
                                      dict(SMALL_DATA, report_layout=layout)))

    players, data = _big_data()
    for layout, all_pilots, max_pilots, top in itertools.product(
            ["R", "S", "D", "DSR", "P", "PR", "RSDP"], [False, True], [None, 5, 40], [1, 10, 50]):
        cfg = {"campaign_name": "C", "max_pilots": max_pilots, "show_pilot_card": True,
               "show_session_card": True, "show_all_pilots": all_pilots, "podium_top": top,
               "podium_days": 0, "podium_combined_top": top, "strip_callsign": all_pilots,
               "show_punishment": True, "show_player_cmd_hint": False}
        key = f"big|{layout}|all={all_pilots}|max={max_pilots}|top={top}"
        results[key] = digest(_render(players, NO_ZONES, cfg, dict(data, report_layout=layout)))

    for top in (1, 10, 50):
        embed = commands.discord.Embed()
        commands._add_podium_field(embed, "👑", commands._build_podium_table(data["daily_history"], players, 0, top))
        results[f"podium_command|top={top}"] = digest(embed_dump(embed))

    stats = {f"Very long map special mission name number {i}": i for i in range(80)}
    seen = datetime(2026, 9, 1, tzinfo=timezone.utc)
    embed = commands._build_player_report_embed("P", {"credits": 5000, "career": {1: 9000, 30: 123456}},
                                                UCID[5], seen, 10, 20, stats, "ok", stats)
    assert len(embed.fields) > 8           # stats table split across several fields
    results["player_report"] = digest(embed_dump(embed))
    check_golden("embeds.json", results)


def test_embed_respects_discord_limits():
    players, data = _big_data()
    cfg = {"report_layout": "RSDP", "show_all_pilots": True, "show_pilot_card": True,
           "show_session_card": True, "podium_days": 0, "podium_combined_top": 50}
    embed = commands.build_embed(NO_ZONES, players, cfg, **dict(data, report_layout="RSDP"))
    assert len(embed.fields) <= 25
    assert all(len(f.value) <= 1024 for f in embed.fields)
    assert commands._embed_size(embed) <= commands.DISCORD_EMBED_LIMIT


def test_version_is_written_as_fh_report_ver_everywhere():
    assert commands._version_text() == f"Fh_Report Ver. {commands.FH_REPORT_RELEASE}"
    main = commands.build_embed({"blue": [], "red": [], "neutral": 0}, {}, {"campaign_name": "C"})
    assert main.footer.text.splitlines()[0] == commands._version_text()
    notstarted = commands.build_not_started_embed({}, {"campaign_name": "C"}, "R", {})
    assert notstarted.footer.text.splitlines()[0] == commands._version_text()
    player = commands._build_player_report_embed("P", {"credits": 1}, None, None, 0, 0, {}, "ok")
    assert player.footer.text.startswith(commands._version_text() + " · ")


# ── max_pilots: one limit per table ────────────────────────────────────────────

def _table_sizes(cfg, layout="DSR"):
    """{table letter: pilots listed} for an embed with plenty of pilots in every table."""
    players, data = _big_data()
    data = {**data, "report_layout": layout}
    embed = commands.build_embed(copy.deepcopy(ZONES), copy.deepcopy(players), {"campaign_name": "C", **cfg},
                                 **copy.deepcopy(data))
    titles = {"Daily Leaderboard": "D", "Session Leaderboard": "S", "Pilot Leaderboard": "R"}
    sizes, current = {}, None
    for f in embed.fields:
        hit = next((t for name, t in titles.items() if name in f.name), None)
        if hit:
            current = hit
            sizes[current] = 0
        if current:
            sizes[current] += sum(1 for line in f.value.splitlines() if line.startswith(("🥇", "🥈", "🥉", "🎖", "•")))
    return sizes


def test_every_table_has_its_own_limit_whatever_the_layout():
    assert _table_sizes({"max_pilots": 4}) == {"D": 4, "S": 4, "R": 4}
    assert _table_sizes({"max_pilots": 4}, layout="R") == {"R": 4}
    assert _table_sizes({"max_pilots": 4, "max_pilots_R": 7, "max_pilots_D": 2}) == {"D": 2, "S": 4, "R": 7}
    assert _table_sizes({"max_pilots_S": 3}) == {"D": _table_sizes({})["D"], "S": 3, "R": _table_sizes({})["R"]}


def test_a_table_does_not_borrow_room_from_the_others():
    """Nothing is shared: a limit of 4 stays 4 even when a table before it is short."""
    sizes = _table_sizes({"max_pilots": 4, "max_pilots_D": 1})
    assert sizes["D"] == 1 and sizes["S"] == 4 and sizes["R"] == 4


def test_the_old_two_and_three_table_keys_are_gone():
    assert _table_sizes({"max_pilots_2t": 2, "max_pilots_3t": 1}) == _table_sizes({})


import pytest


@pytest.mark.parametrize("value, expected", [
    (None, None), ("", None), (15, 15), ("15", 15), (" 7 ", 7), (0, None), ("0", None),
    (-3, None), ("-3", None), ("many", None), (4.5, None), (True, None), ([5], None),
])
def test_pilot_limit_values(value, expected):
    assert commands._pilot_cap({"max_pilots": value}, "R") == expected


def test_pilot_limit_resolution_order():
    cfg = {"max_pilots": 10, "max_pilots_S": 3, "max_pilots_D": 0}
    assert commands._pilot_cap(cfg, "R") == 10            # the default
    assert commands._pilot_cap(cfg, "S") == 3             # its own
    assert commands._pilot_cap(cfg, "D") is None          # its own 0 = no limit, ignoring max_pilots
    assert commands._pilot_cap({"max_pilots_R": "x", "max_pilots": 5}, "R") is None   # a bad own value is not papered over


def _tag(nodes, remote):
    from conftest import make_plugin
    srv = type("S", (), {"is_remote": remote})()
    node = type("N", (), {"all_nodes": {f"n{i}": None for i in range(nodes)}})()
    return make_plugin(bot=type("B", (), {"node": node})())._where_tag(srv)


def test_footer_says_where_the_server_runs_only_on_a_cluster():
    assert _tag(1, False) is None and _tag(1, True) is None            # a single node needs no tag
    assert _tag(2, False) == "M" and _tag(3, True) == "N"
    assert commands._version_text("M") == f"Fh_Report Ver. {commands.FH_REPORT_RELEASE} • M"
    assert commands._version_text() == f"Fh_Report Ver. {commands.FH_REPORT_RELEASE}"


def test_footer_tag_reaches_every_embed():
    cfg, zones = {"campaign_name": "C"}, {"blue": [], "red": [], "neutral": 0}
    assert commands.build_not_started_embed({}, cfg, "R", {}, where="N").footer.text.splitlines()[0].endswith("• N")
    assert commands.build_embed(zones, {}, cfg, where="M").footer.text.splitlines()[0].endswith("• M")
    assert "•" not in commands.build_embed(zones, {}, cfg).footer.text.splitlines()[0]
    player = commands._build_player_report_embed("P", {"credits": 1}, None, None, 0, 0, {}, "ok", where="N")
    assert player.footer.text.startswith(commands._version_text("N") + " · ")


def test_every_pilot_gets_a_medal_and_only_the_top_three_differ():
    import discord
    players = {f"P{i:03d}": {"credits": 100000 - i, "ucid": f"u{i}"} for i in range(70)}
    embed = discord.Embed(title="t")
    commands._render_layout_tables(embed, {"max_pilots": 0, "show_all_pilots": True}, "R", {}, players, {}, False, None, None, None)
    lines = [l for f in embed.fields for l in f.value.splitlines()
             if l.startswith(("🥇", "🥈", "🥉", "🎖", "•"))]
    assert len(lines) == 70 and not any(l.startswith("•") for l in lines)       # past the 53rd too
    assert [l[0] for l in lines[:3]] == ["🥇", "🥈", "🥉"] and all(l.startswith("🎖") for l in lines[3:])


def test_older_podium_days_use_the_players_own_medal_but_the_latest_day_never_does():
    players = {"Ann": {"credits": 1000, "ucid": "ua", "custom_medal": "🦅"},
               "Bob": {"credits": 900, "ucid": "ub"}}
    history = {"2026-08-02": [{"top": [{"name": "Ann", "points": 50, "ucid": "ua"},
                                       {"name": "Bob", "points": 40, "ucid": "ub"}]}],
               "2026-08-01": [{"top": [{"name": "Ann", "points": 30, "ucid": "ua"},
                                       {"name": "Bob", "points": 20, "ucid": "ub"},
                                       {"name": "Ghost", "points": 10}]}]}
    text = commands._build_podium_table(history, players, days=0, top=3)
    latest, older = text.split("\n")[:3], text.split("\n")[3:]
    assert latest[1].startswith("🥇") and latest[2].startswith("🥈")
    assert older[1].startswith("🦅")
    assert older[2].startswith("🥈") and older[3].startswith("🥉")
