"""Rendering: every option combination must keep producing exactly the same
embed (golden file). Covers zones, all report_layout tables, Podium, cards,
punishment badges, field chunking and the /fh_report player embed."""
import copy
import itertools
import random
from datetime import datetime, timezone

from conftest import UCID, check_golden, commands, digest, embed_dump


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
