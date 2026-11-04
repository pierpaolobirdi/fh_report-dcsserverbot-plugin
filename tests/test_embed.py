"""Rendering: every option combination must keep producing exactly the same
embed (golden file). Covers zones, all report_layout tables, Podium, cards,
punishment badges, field chunking and the /fh_report player embed."""
import asyncio
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
    monkeypatch.setattr(commands, "PLUGIN_RELEASE", "12.5.4")


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
    assert commands._version_text() == f"Fh_Report Ver. {commands.PLUGIN_RELEASE}"
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
    assert commands._version_text("M") == f"Fh_Report Ver. {commands.PLUGIN_RELEASE} • M"
    assert commands._version_text() == f"Fh_Report Ver. {commands.PLUGIN_RELEASE}"


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
    embed = commands.discord.Embed(title="t")
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


def test_podium_orders_each_day_by_points_whatever_the_file_order():
    history = {"2026-10-10": [{"top": [{"name": "Dax", "points": 856},
                                       {"name": "Vip", "points": 1165},
                                       {"name": "Asa", "points": 432}]}]}
    lines = commands._build_podium_table(history, {}, days=0, top=3).split("\n")[1:]
    assert "Vip" in lines[0] and "Dax" in lines[1] and "Asa" in lines[2]


# ── the Discord avatar on the player card ───────────────────────────────────────────

def _card(**kw):
    return commands._build_player_report_embed("P", {"credits": 5000}, kw.pop("ucid", "u" * 32), None, 0, 0, {}, "ok", **kw)


def test_the_player_card_has_ucid_and_a_short_ruler_in_the_description_and_the_avatar_on_the_right():
    card = _card(avatar_url="https://cdn/avatar.png")
    assert card.thumbnail_url == "https://cdn/avatar.png"
    assert card.description == "\u200b\n🔑 UCID: " + "u" * 32 + "\n" + "▬" * commands.PLAYER_SEPARATOR_LEN
    assert commands._SEPARATOR == "▬" * commands.PLAYER_SEPARATOR_LEN       # every ruler of the card is that short
    assert not hasattr(_card(), "thumbnail_url")                              # no avatar: no thumbnail
    assert _card(ucid=None).description == "▬" * commands.PLAYER_SEPARATOR_LEN


class _Member:
    display_avatar = type("A", (), {"url": "https://cdn/a.png"})()


def _avatar(bot, ucid="x1", monkeypatch=None):
    commands._AVATAR_CACHE.clear()
    return asyncio.run(commands._discord_avatar_url(bot, ucid))


def test_the_avatar_comes_from_the_linked_discord_user_and_is_cached(monkeypatch):
    calls = []

    class Bot:
        async def get_member_by_ucid(self, ucid):
            calls.append(ucid)
            return _Member()
    commands._AVATAR_CACHE.clear()
    assert asyncio.run(commands._discord_avatar_url(Bot(), "x1")) == "https://cdn/a.png"
    assert asyncio.run(commands._discord_avatar_url(Bot(), "x1")) == "https://cdn/a.png" and calls == ["x1"]   # cached


def test_no_link_no_ucid_a_failing_or_a_slow_lookup_just_means_no_avatar(monkeypatch):
    class NoLink:
        async def get_member_by_ucid(self, ucid):
            return None

    class Broken:
        async def get_member_by_ucid(self, ucid):
            raise RuntimeError("db down")

    class Slow:
        async def get_member_by_ucid(self, ucid):
            await asyncio.sleep(10)

    assert _avatar(NoLink()) is None and _avatar(Broken()) is None and _avatar(None) is None
    assert asyncio.run(commands._discord_avatar_url(NoLink(), None)) is None
    real_wait = asyncio.wait_for
    monkeypatch.setattr(commands.asyncio, "wait_for", lambda coro, timeout: real_wait(coro, 0.01))
    assert _avatar(Slow()) is None
    assert "x1" not in commands._AVATAR_CACHE                                   # failures are not remembered


# ── the player card must not wrap next to the avatar ──────────────────────────────────

def _widest(table):
    return max(len(line) for line in table.strip("`\n").split("\n"))


def test_every_ruler_of_the_player_card_is_short_enough():
    stats = {"Points": 1, "Air": 11, "Ground Units": 38, "Achievement": 2}
    card = commands._build_player_report_embed("P", {"credits": 5000}, "u" * 32, None, 10, 20, stats, "ok", stats,
                                              bnb={"total": 1, "session": 1, "day": 1, "destroyed": 1, "damaged": 0,
                                                   "recent": []},
                                              penalties={"total": 5, "rows": [("kill", 1, 5.0)]})
    rulers = [f.value for f in card.fields if f.value.strip() and set(f.value) == {"▬"}]
    assert rulers and all(len(r) <= commands.PLAYER_SEPARATOR_LEN for r in rulers)


def test_long_stat_names_are_shortened_so_the_table_fits():
    session = {"Strike known Bin Laden location": 1, "Destroy the EWR at Nimroz": 1, "Air": 11, "Points spent": 1700}
    table = commands._build_combined_stats_table(session, {"Air": 2})
    assert _widest(table) <= commands.PLAYER_TABLE_WIDTH == 44
    assert ".." in table and "Category" in table and "Session" in table and "Daily" in table
    lines = table.strip("`\n").split("\n")
    assert len({len(line) for line in lines if not set(line) <= {"-"}}) == 1          # still aligned
    assert "1,700" in table and "Air" in table                                           # values and short names intact


def test_a_table_that_already_fits_is_left_as_it_was():
    table = commands._build_combined_stats_table({"Air": 3, "Helo": 1}, {"Air": 1})
    assert ".." not in table and "Air" in table and "Helo" in table
    assert commands._fit_label("Air", 10) == "Air" and commands._fit_label("Strike on enemy EWR", 10) == "Strike o.."


def _title(session_points, daily_points, daily=True):
    stats = {"Points": 1, "Air": 3}
    card = commands._build_player_report_embed("P", {"credits": 5000}, None, None, session_points, daily_points,
                                              stats, "ok", stats if daily else None)
    return next(f.name for f in card.fields if "Session Stats" in f.name)


def test_the_spaces_above_the_table_are_worked_out_from_the_numbers():
    gaps = {}
    for s, d in ((8864, 0), (8864, 123456), (999999, 999999), (5, 5), (123456789, 123456789)):
        title = _title(s, d)
        left, right = title.split("  ", 1)[0], title.split("  ", 1)[1].lstrip(" ")
        gap = len(title) - len(left) - len(right)
        assert gap >= 2
        gaps[(s, d)] = gap
        width = commands._text_width(title)
        assert width <= commands.PLAYER_TITLE_WIDTH or gap == 2             # never wider, unless already at the minimum
    assert gaps[(8864, 0)] == 42                                             # the old 44 less the safety margin
    assert gaps[(8864, 123456)] < gaps[(8864, 0)]                            # a bigger daily total takes spaces away
    assert gaps[(5, 5)] > gaps[(8864, 0)]                                    # smaller numbers leave more


def test_without_daily_stats_the_title_is_just_the_session_part():
    assert "Daily" not in _title(8864, 0, daily=False)


# ── pilots with nothing scored yet but a BoB or a punishment badge are listed in Session ──

def _session_text(players, cfg, pp=None, layout="S"):
    embed = commands.discord.Embed(title="t")
    commands._render_layout_tables(embed, cfg, layout, {}, players, {}, False, pp or {}, None)
    return "\n".join(f.value for f in embed.fields)


def _pilot(name, pts=0, **kw):
    return name, {"credits": 100, "session_points": pts, "ucid": "u" + name, **kw}


def test_session_lists_a_pilot_with_only_bob_or_a_punishment_badge_after_the_scorers():
    players = dict([_pilot("Ann", 50), _pilot("Bob", 0, bnb={"session": 2}), _pilot("Cy", 0),
                    _pilot("Dee", 0)])
    cfg = {"show_session_card": True, "show_punishment": True}
    text = _session_text(players, cfg, pp={"uDee": 40})
    assert "Ann" in text and "Bob" in text and "Dee" in text and "Cy" not in text
    assert text.index("Ann") < text.index("Dee") < text.index("Bob")           # scorers, then punished, then BoB
    assert "2 BoB" in text and "🔨" in text and "S: 0" not in text


def test_session_keeps_zero_point_pilots_out_when_the_features_are_off():
    players = dict([_pilot("Ann", 50), _pilot("Bob", 0, bnb={"session": 2}), _pilot("Dee", 0)])
    text = _session_text(players, {"show_session_card": False, "show_punishment": False}, pp={"uDee": 40})
    assert "Ann" in text and "Bob" not in text and "Dee" not in text


def test_the_punished_pilot_only_counts_in_the_table_that_carries_the_badges():
    players = dict([_pilot("Ann", 50), _pilot("Dee", 0)])
    embed = commands.discord.Embed(title="t")
    commands._render_layout_tables(embed, {"show_punishment": True}, "RS", {}, players, {}, False, {"uDee": 40}, None)
    rank, session = embed.fields[0].value, embed.fields[-1].value               # R carries the badges
    assert "Dee" in rank and "🔨" in rank
    assert "Ann" in session and "Dee" not in session


# ── pilots only the campaign save knows (no record in Foothold_Ranks.lua) ───────────────────

def test_unranked_pilots_are_added_from_the_save_with_their_ucid_and_without_credits():
    players = {"Ann": {"credits": 500.0, "ucid": "ua", "career": {}}}
    camp = {"Ann": 10, "Bob": 891, "Cy": 0, "Dee": 40, "Ann2": 5}
    sess = {"Cy": {"Air": 1}, "Eve": {}}
    added = commands.add_unranked_pilots(
        players, camp, sess, native_ucids={"Dee": "ud"}, identities={"Bob": "ub", "Ann2": "ua"}, excluded_ucids=["ud"])
    assert added == ["Bob", "Cy"]                         # Dee is excluded, Ann2 is Ann (same UCID), Eve has nothing
    assert players["Bob"] == {"credits": 0.0, "ucid": "ub", "career": {}, "unranked": True, "hide_credits": True}
    assert players["Ann"]["credits"] == 500.0 and "unranked" not in players["Ann"]
    again = commands.add_unranked_pilots(
        players, camp, sess, native_ucids={"Dee": "ud"}, identities={"Bob": "ub", "Ann2": "ua"}, excluded_ucids=["ud"])
    assert again == []                                     # nothing new the second time


def test_show_unranked_pilots_is_on_unless_switched_off():
    assert commands._show_unranked({}) and commands._show_unranked({"show_unranked_pilots": True})
    assert not commands._show_unranked({"show_unranked_pilots": False})
    assert not commands._show_unranked({"show_unranked_pilots": "false"})


def test_session_and_daily_list_an_unranked_pilot_without_a_rank_and_rank_skips_it():
    players = {"Ann": {"credits": 500.0, "ucid": "ua", "career": {}, "session_points": 20},
               "Bob": {"credits": 0.0, "ucid": "ub", "career": {}, "unranked": True, "hide_credits": True,
                       "session_points": 891}}
    embed = commands.discord.Embed(title="t")
    commands._render_layout_tables(embed, {}, "SR", {}, players, {"Ann": 5, "Bob": 7}, True, {}, None)
    session, rank = embed.fields[0].value, embed.fields[1].value
    assert "Bob" in session and "(S: 891)" in session and "Recruit" not in session.split("Bob")[1].split("\n")[0]
    assert "**" not in session.split("Bob")[1].split("\n")[0]          # no bold rank after the name
    assert "Bob" not in rank and "Ann" in rank


def test_the_podium_shows_an_unranked_pilot_without_a_rank():
    players = {"Bob": {"credits": 0.0, "ucid": "ub", "career": {}, "unranked": True}}
    history = {"2026-09-01": [{"top": [{"name": "Bob", "points": 438, "ucid": "ub"}]}]}
    text = commands._build_podium_table(history, players, days=0, top=1)
    assert "Bob" in text and "438 pts" in text and "**" not in text


# ── the punishment badge ──────────────────────────────────────────────────────────────────────

def test_a_custom_icon_replaces_the_hammers_instead_of_being_repeated():
    assert commands.get_punishment_badge(30) .endswith("🔨🔨🔨")                              # level 3 = three hammers
    assert commands.get_punishment_badge(30, "", "🍹🍹").endswith("(30 p.p.) 🍹🍹")        # exactly what was written
    assert commands.get_punishment_badge(0) is None
    plain = commands.get_punishment_badge(0, "", "🔨🔨🔨", "Late", "🥁", show_points=False)
    assert plain == "·　🥁 Late 🔨🔨🔨" and "p.p." not in plain
    assert commands.get_punishment_badge(0, show_points=False) == "·　🧿 JAG's watch 🔨"      # the lowest level


def test_the_badge_of_a_pilot_follows_its_points_and_its_own_look():
    f = commands.punishment_for
    assert f({}, 0) == (None, 0.0) and f({"punishment_icon": ""}, 0) == (None, 0.0)
    assert f({}, 2)[0] == "·　🧿 JAG's watch (2 p.p.) 🔨"
    own = {"punishment_pre_icon": "🥁", "punishment_label": "Rowdy", "punishment_icon": "🔨🔨🔨"}
    assert f(own, 0) == ("·　🥁 Rowdy 🔨🔨🔨", 0.0)                                           # a look of its own is enough
    assert f(own, 30)[0] == "·　🥁 Rowdy (30 p.p.) 🔨🔨🔨"                                   # with real points, they show
    assert f({**own, "hook_punishment": 0}, 30) == (None, 0.0)                              # 0 = forced to none
    assert f({**own, "hook_punishment": 12}, 0)[0] == "·　🥁 Rowdy (12 p.p.) 🔨🔨🔨"         # a points value of its own
    for yes in (True, "x", "yes"):
        assert f({**own, "hook_punishment": yes}, 0) == ("·　🥁 Rowdy 🔨🔨🔨", 0.0)            # no number: shown without points
    for no in (False, "no", "off"):
        assert f({**own, "hook_punishment": no}, 30) == (None, 0.0)
    assert f({**own, "hook_punishment": ""}, 5)[0].endswith("(5 p.p.) 🔨🔨🔨")                # empty = real points


def test_the_table_shows_the_badge_of_a_pilot_with_a_look_of_its_own_and_no_points():
    own = {"punishment_pre_icon": "🥁", "punishment_label": "Rowdy", "punishment_icon": "🔨🔨🔨"}
    players = dict([_pilot("Ann", 50), _pilot("Bob", 20, **own), _pilot("Cy", 10)])
    embed = commands.discord.Embed(title="t")
    commands._render_layout_tables(embed, {"show_punishment": True}, "S", {}, players, {}, False, {}, None)
    text = embed.fields[0].value
    assert text.count("🥁 Rowdy 🔨🔨🔨") == 1 and "p.p." not in text
    assert text.index("Bob") < text.index("🥁") < text.index("Cy")                           # under Bob, not under others
    zero = dict([_pilot("Ann", 50), _pilot("Dee", 0, **own)])                                # with nothing scored it is listed too
    embed = commands.discord.Embed(title="t")
    commands._render_layout_tables(embed, {"show_punishment": True}, "S", {}, zero, {}, False, {}, None)
    assert "Dee" in embed.fields[0].value and "🥁 Rowdy" in embed.fields[0].value
