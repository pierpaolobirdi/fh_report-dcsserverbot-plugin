"""Campaign history: one entry per campaign, with the final season table, kept in .fhc/fhr_session_history.json."""
import asyncio
import json
from datetime import timedelta

from conftest import commands, make_plugin
from test_session import IDS, T0, _Clock, _Game, _Server

NAMES = {i: f"Pilot{n}" for n, i in enumerate(IDS)}


def _pts(scale=1.0, ids=IDS[:3]):
    return {i: int(1000 * scale * (n + 1)) for n, i in enumerate(ids)}


class _Hist(_Game):
    def cycle(self, minutes, points, name="foothold_x.lua", map_name="Syria", min_points=200, kills=None):
        self.clock.at(minutes)
        node = commands._UpdateReadCache(self.source)
        kills = kills if kills is not None else {i: 10 for i in points}
        asyncio.run(self.plugin._update_session(
            _Server(), str(self.dir), node, self.source, str(self.dir / name), points, kills))
        asyncio.run(self.plugin._track_campaign(
            _Server(), str(self.dir), self.source, points, NAMES, {"session_history_min_points": min_points}, map_name))

    def history(self):
        return json.loads((self.dir / ".fhc" / "fhr_session_history.json").read_text())["sessions"]

    def restart_bot(self):
        self.plugin = make_plugin(apool=self.pool)


def test_the_first_look_opens_an_entry_with_the_whole_season_table(tmp_path, monkeypatch):
    g = _Hist(tmp_path, monkeypatch)
    g.cycle(0, _pts())
    [entry] = g.history()
    assert entry["open"] and entry["file"] == "foothold_x.lua" and entry["map"] == "Syria"
    assert entry["start_kind"] == "first_seen" and entry["end"] is None and entry["total"] == 6000
    assert entry["players"][IDS[2]] == {"name": "Pilot2", "points": 3000}
    assert [r["name"] for r in commands.campaign_ranking(entry)] == ["Pilot2", "Pilot1", "Pilot0"]


def test_a_changed_table_is_written_every_quarter_of_an_hour_not_every_cycle(tmp_path, monkeypatch):
    g = _Hist(tmp_path, monkeypatch)
    g.cycle(0, _pts())
    g.cycle(5, _pts(2))
    assert g.history()[0]["total"] == 6000                         # held back
    g.cycle(16, _pts(3))
    assert g.history()[0]["total"] == 18000


def test_a_map_change_closes_the_campaign_with_its_last_table_and_opens_the_next(tmp_path, monkeypatch):
    g = _Hist(tmp_path, monkeypatch)
    g.cycle(0, _pts())
    g.cycle(5, _pts(2))
    g.pool.mission_start = T0 + timedelta(minutes=6)
    g.cycle(10, {}, name="FootHold_CA_v0.3.lua", map_name="Caucasus")
    old, new = g.history()
    assert not old["open"] and old["closed_by"] == "map" and old["total"] == 12000
    assert old["end"] == (T0 + timedelta(minutes=6)).strftime("%Y-%m-%dT%H:%M:%S")
    assert new["open"] and new["file"] == "FootHold_CA_v0.3.lua" and new["map"] == "Caucasus"


def test_coming_back_to_a_map_without_losing_points_reopens_its_entry(tmp_path, monkeypatch):
    g = _Hist(tmp_path, monkeypatch)
    g.cycle(0, _pts())
    g.cycle(10, _pts(0.2, IDS[3:5]), name="foothold_y.lua", map_name="Caucasus", min_points=100)
    g.cycle(20, _pts(1.5), min_points=100)                          # Syria again, points grew
    syria, other = g.history()
    assert len(g.history()) == 2 and syria["open"] and not other["open"]
    assert syria["end"] is None and syria["total"] == 9000 and syria["start_kind"] == "first_seen"


def test_coming_back_to_a_map_that_was_reset_is_a_new_campaign(tmp_path, monkeypatch):
    g = _Hist(tmp_path, monkeypatch)
    g.cycle(0, _pts())
    g.cycle(10, _pts(0.2, IDS[3:5]), name="foothold_y.lua", map_name="Caucasus", min_points=100)
    g.cycle(20, {IDS[0]: 50, IDS[1]: 80}, min_points=100)           # Syria again, started over
    syria_old, other, syria_new = g.history()
    assert not syria_old["open"] and syria_old["total"] == 6000
    assert syria_new["open"] and syria_new["id"] != syria_old["id"]


def test_a_reset_in_the_same_file_closes_with_the_last_intact_table(tmp_path, monkeypatch):
    g = _Hist(tmp_path, monkeypatch)
    g.cycle(0, _pts())
    g.cycle(5, _pts(2))
    g.cycle(10, {})                                                 # first look: could be a glitch, table kept
    assert g.history()[0]["open"] and g.history()[0]["total"] == 6000   # (not written yet: nothing changed)
    g.pool.mission_start = T0 + timedelta(minutes=12)
    g.cycle(15, {})                                                 # second look confirms
    old, new = g.history()
    assert not old["open"] and old["closed_by"] == "reset" and old["total"] == 12000
    assert new["open"] and new["total"] == 0


def test_a_campaign_below_the_minimum_is_not_kept(tmp_path, monkeypatch):
    g = _Hist(tmp_path, monkeypatch)
    g.cycle(0, {IDS[0]: 50, IDS[1]: 60})
    g.cycle(10, _pts(), name="foothold_y.lua", map_name="Caucasus")
    assert [c["file"] for c in g.history()] == ["foothold_y.lua"]


def test_the_history_survives_a_bot_restart(tmp_path, monkeypatch):
    g = _Hist(tmp_path, monkeypatch)
    g.cycle(0, _pts())
    g.restart_bot()
    g.cycle(5, _pts(2))
    g.cycle(20, _pts(3), name="foothold_y.lua", map_name="Caucasus", min_points=100)
    old, new = g.history()
    assert old["id"].startswith("foothold_x@") and not old["open"] and new["open"]


def test_the_excluded_pilots_are_not_in_the_table():
    table = commands._campaign_table({"a": 100, "b": 5, "c": 0}, {"a": "A"}, ["b"])
    assert table == {"a": {"name": "A", "points": 100}}


# ── the H block ────────────────────────────────────────────────────────────────

def _entry(file, start, end, table, kind="detected", map_name="", open_=False):
    players = {pid: {"name": name, "points": pts} for pid, name, pts in table}
    return {"id": f"{file}@{start}", "file": file, "map": map_name, "start": start, "start_kind": kind, "session": start,
            "end": end, "closed_by": "map", "open": open_, "players": players,
            "total": sum(pts for _, _, pts in table)}


HISTORY = {"sessions": [
    _entry("foothold_syria.lua", "2026-09-01T10:00:00", "2026-09-20T10:00:00", [("a", "Zarpa", 28400), ("b", "Viper", 900)], kind="first_seen", map_name="Syria"),
    _entry("foothold_caucasus.lua", "2026-09-20T10:00:00", "2026-10-12T10:00:00", [("b", "Viper", 35900), ("a", "Zarpa", 100)], map_name="Caucasus"),
    _entry("foothold_syria.lua", "2026-10-12T10:00:00", "2026-11-03T10:00:00",
           [("a", "Zarpa", 41230), ("b", "Viper", 30110), ("c", "Excl", 22004), ("d", "Fourth", 5)], map_name="Syria"),
    _entry("foothold_syria.lua", "2026-11-03T10:00:00", None, [("a", "Zarpa", 7)], map_name="Syria", open_=True),
]}


def test_the_newest_session_lists_its_places_and_the_older_ones_one_line(tmp_path):
    blocks = commands._campaign_blocks(HISTORY, {}, count=3, latest_top=3, top=1)
    assert len(blocks) == 3                                            # the running campaign is not listed
    newest, middle, oldest = blocks
    assert newest[0].endswith("__ SYRIA") and "→" in newest[0] and len(newest) == 4
    assert newest[1] == "🥇 `Zarpa` — 41,230 pts" and newest[3] == "🥉 `Excl` — 22,004 pts"
    assert len(middle) == 1 and middle[0].endswith("__ CAUCASUS 🥇 `Viper` — 35,900 pts") and "→" not in middle[0]
    assert len(oldest) == 1 and oldest[0].endswith("__ SYRIA 🥇 `Zarpa` — 28,400 pts")


def test_count_and_places_follow_the_options():
    assert len(commands._campaign_blocks(HISTORY, {}, count=2, latest_top=1, top=1)) == 2
    assert len(commands._campaign_blocks(HISTORY, {}, count=0, latest_top=1, top=1)) == 3
    only_champions = commands._campaign_blocks(HISTORY, {}, count=3, latest_top=1, top=1)
    assert all(len(b) == 1 for b in only_champions)
    two_each = commands._campaign_blocks(HISTORY, {}, count=3, latest_top=2, top=2)
    assert [len(b) for b in two_each] == [3, 3, 3]


def test_an_estimated_start_is_marked_and_a_pilot_found_by_ucid_shows_the_current_name_and_rank():
    history = {"sessions": [_entry("foothold_syria.lua", "2026-09-01T10:00:00", "2026-09-20T10:00:00",
                                    [("u1", "OldName", 28400), ("u2", "Gone", 100)], kind="first_seen", map_name="Syria")]}
    players = {"NewName": {"ucid": "u1", "credits": 5000}}
    [[head, first, second]] = commands._campaign_blocks(history, players, count=3, latest_top=2, top=1)
    assert head.startswith("__~<t:")
    assert first.startswith("🥇 `NewName` — **") and first.endswith("— 28,400 pts")
    assert second == "🥈 `Gone` — 100 pts"                           # not in the ranks: as recorded, no rank


def test_the_block_is_a_field_of_the_embed_and_nothing_is_shown_without_campaigns():
    cfg = {"report_layout": "RH", "session_history_count": 2}
    embed = commands.build_embed({"blue": [], "red": [], "neutral": 0}, {}, cfg, report_layout="RH",
                                 campaign_history=HISTORY)
    [field] = [f for f in embed.fields if "Session History" in f.name]
    assert field.name.startswith("🏁") and field.value.count("\n") == 4
    bare = commands.build_embed({"blue": [], "red": [], "neutral": 0}, {}, cfg, report_layout="RH")
    assert not [f for f in bare.fields if "Session History" in f.name]


def test_a_map_name_is_made_from_the_file_when_dcssb_never_reported_one():
    assert commands._campaign_map({"file": "foothold_afghanistan.lua"}) == "AFGHANISTAN"
    assert commands._campaign_map({"file": "x.lua", "map": "Syria"}) == "SYRIA"


def test_the_maps_are_shown_in_capitals_under_the_names_foothold_goes_by():
    names = {"Caucasus": "CAUCASUS", "Kola": "KOLA", "Syria": "SYRIA", "PersianGulf": "PERSIAN GULF",
             "SinaiMap": "SINAI", "Sinai": "SINAI", "Iraq": "IRAK", "GermanyCW": "GERMANY", "Normandy": "NORMANDY",
             "PERSIAN GULF": "PERSIAN GULF", "TheChannel": "THE CHANNEL", "MarianaIslands": "MARIANA ISLANDS",
             "": "?", None: "?"}
    assert {k: commands._map_label(k) for k in names} == names


# ── the filters and the statistics ──────────────────────────────────────────────────────────

import os                                                    # noqa: E402
from datetime import date                                    # noqa: E402

from conftest import FileNode                                # noqa: E402

UTC = commands.timezone.utc


def test_the_filters_combine_and_the_answer_is_oldest_first():
    everything = commands.campaign_filter(HISTORY)
    assert [c["end"][:10] for c in everything] == ["2026-09-20", "2026-10-12", "2026-11-03"]   # the running one is not there
    assert [c["map"] for c in commands.campaign_filter(HISTORY, map_name="syr")] == ["Syria", "Syria"]
    inside = commands.campaign_filter(HISTORY, date_from=date(2026, 10, 1), date_to=date(2026, 10, 31))
    assert [c["map"] for c in inside] == ["Caucasus"]
    assert len(commands.campaign_filter(HISTORY, pilot="excl")) == 1                          # part of a name
    assert len(commands.campaign_filter(HISTORY, pilot="b")) == 3                             # a UCID, exactly
    assert commands.campaign_filter(HISTORY, map_name="syria", pilot="viper", date_from=date(2026, 11, 4)) == []


def test_the_dates_are_those_of_the_servers_time_zone():
    late = {"sessions": [_entry("foothold_syria.lua", "2026-10-01T10:00:00", "2026-10-31T23:30:00", [("a", "Z", 500)])]}
    madrid = commands._tz_from_name("Europe/Madrid")
    assert commands.campaign_filter(late, date_to=date(2026, 10, 31), tz=UTC)
    assert not commands.campaign_filter(late, date_to=date(2026, 10, 31), tz=madrid)        # already 1 November there


def test_the_champions_table_counts_wins_podiums_and_the_best_score():
    rows = commands.campaign_champions(commands.campaign_filter(HISTORY))
    zarpa, viper = rows[0], rows[1]
    assert (zarpa["name"], zarpa["wins"], zarpa["podiums"], zarpa["played"], zarpa["best"]) == ("Zarpa", 2, 3, 3, 41230)
    assert zarpa["best_map"] == "SYRIA"
    assert (viper["name"], viper["wins"], viper["podiums"], viper["best"], viper["best_map"]) == ("Viper", 1, 3, 35900, "CAUCASUS")
    assert commands._champion_lines(rows, 2)[0].startswith("🥇 `Zarpa` — **2** wins · 3 podiums · 3 sessions · best 41,230 pts (SYRIA)")
    assert len(commands._champion_lines(rows, 2)) == 2


def test_a_pilots_record_lists_position_and_points_newest_first():
    record = commands.campaign_pilot_history(commands.campaign_filter(HISTORY), "viper")
    assert [(h["position"], h["of"], h["points"]) for h in record] == [(2, 2, 900), (1, 2, 35900), (2, 4, 30110)]
    lines = commands._pilot_record_lines(record)
    assert lines[0] == "03-11-2026 · SYRIA — **2** of 4 — 30,110 pts"


def test_the_table_of_one_campaign_adds_the_pilot_asked_for_when_outside_the_top():
    newest = commands.campaign_filter(HISTORY)[-1]
    assert commands._campaign_detail_lines(newest, 2) == ["🥇 `Zarpa` — 41,230 pts", "🥈 `Viper` — 30,110 pts"]
    assert commands._campaign_detail_lines(newest, 2, "fourth")[-2:] == ["…", "`4.` `Fourth` — 5 pts"]
    assert commands._campaign_length(newest) == "22 d 0 h"
    assert commands._campaign_label(newest) == "SYRIA · 12-10-2026 → 03-11-2026"


# ── the command ────────────────────────────────────────────────────────────────────────────────

class _Followup:
    def __init__(self):
        self.sent = []

    async def send(self, content=None, embed=None, view=None, ephemeral=None):
        self.sent.append((content, embed, view, ephemeral))
        return object()


class _Response:
    deferred = None

    async def defer(self, ephemeral=None):
        self.deferred = ephemeral


def _command(tmp_path, history=HISTORY, admin=True, **kw):
    d = str(tmp_path)
    os.makedirs(os.path.join(d, ".fhc"), exist_ok=True)
    with open(os.path.join(d, ".fhc", "fhr_session_history.json"), "w") as f:
        json.dump(history, f)
    srv = type("S", (), {"node": FileNode(), "status": commands.Status.RUNNING, "name": "Public",
                         "instance": type("N", (), {"name": "inst"})()})()
    plugin = make_plugin()
    plugin._resolve_server = lambda interaction, param: ("inst", None)
    plugin._server_by_key = lambda key: srv
    plugin._is_admin = lambda interaction, key: admin
    plugin._merged_cfg = lambda key: {"saves_dir": d}
    plugin._public_server_name = lambda key: "Public"
    plugin._reset_tz = lambda server: UTC
    plugin._where_tag = lambda server: None
    interaction = type("I", (), {"followup": _Followup(), "response": _Response(), "user": object()})()
    asyncio.run(plugin.sessions(interaction, **kw))
    return interaction


def test_only_admins_can_use_it_and_the_answer_is_always_private(tmp_path):
    refused = _command(tmp_path, admin=False)
    assert refused.followup.sent[0][0].startswith("❌ Only admins") and refused.response.deferred is True
    answered = _command(tmp_path)
    assert answered.followup.sent[0][3] is True


def test_the_list_shows_the_campaigns_newest_first_with_the_filters_in_the_description(tmp_path):
    [(_, embed, view, _e)] = _command(tmp_path, map_name="Syria", top=2).followup.sent
    assert embed.description == "2 session(s) · map Syria · newest first"
    text = embed.fields[0].value
    assert text.index("Zarpa` — 41,230") < text.index("28,400") and view is None
    [(_, embed, _v, _e)] = _command(tmp_path, order="oldest", top=1).followup.sent
    assert embed.fields[0].value.index("Zarpa` — 28,400") < embed.fields[0].value.index("Viper` — 35,900")


def test_detail_picks_the_latest_finished_campaign_or_the_one_asked_for(tmp_path):
    [(_, embed, _v, _e)] = _command(tmp_path, view="detail", top=3).followup.sent
    assert "SYRIA · 12-10-2026 → 03-11-2026" in embed.description and "4 pilots" in embed.description
    assert "ended by map change" in embed.description
    running = HISTORY["sessions"][-1]["id"]
    [(_, embed, _v, _e)] = _command(tmp_path, view="detail", session=running).followup.sent
    assert "running" in embed.description
    [(content, *_)] = _command(tmp_path, view="detail", session="nope").followup.sent
    assert "No session found" in content


def test_champions_shows_the_table_or_one_pilots_record(tmp_path):
    [(_, embed, _v, _e)] = _command(tmp_path, view="champions").followup.sent
    assert embed.title.startswith("🏁 Champions") and "`Zarpa` — **2** wins" in embed.fields[0].value
    [(_, embed, _v, _e)] = _command(tmp_path, view="champions", pilot="b").followup.sent
    value = embed.fields[0].value
    assert embed.title.startswith("🏁 Pilot record") and value.startswith("`Viper` — **1** win · 3 podiums ·") and value.count("\n") == 3


def test_a_bad_date_or_no_match_is_explained(tmp_path):
    [(content, *_)] = _command(tmp_path, date_from="31-02-2026").followup.sent
    assert "couldn't read `31-02-2026`" in content
    [(content, *_)] = _command(tmp_path, map_name="Nevada").followup.sent
    assert "none matches the filters" in content
    [(content, *_)] = _command(tmp_path, history={"sessions": []}).followup.sent
    assert "Nothing has been recorded yet" in content


def test_every_text_of_the_command_fits_discords_100_characters():
    """Discord refuses to register a command whose description or option help is longer."""
    import re
    src = open(commands.__file__, encoding="utf-8").read()
    block = src[src.index('@fh_report.command(name="sessions"'):src.index("async def sessions")]
    assert all(len(m.group(2)) <= 100 for m in re.finditer(r'(\w+)="([^"]+)"', block))


def _visible(line: str) -> str:
    """What a reader sees of a line: no Markdown, and a Discord date as its 10 characters."""
    import re
    return re.sub(r"<t:\d+:d>", "0" * 10, line).replace("__", "").replace("**", "").replace("`", "")


def test_a_long_rank_is_cut_so_the_whole_line_stays_under_78_characters():
    long_rank = "PILOTO POR INSTINTO DE COMBATE MUY LARGO Y MAS"
    players = {"Eskuvy": {"ucid": "a", "credits": 5000, "custom_rank": long_rank},
               "Pier_Paolo": {"ucid": "b", "credits": 5000, "custom_rank": long_rank}}
    history = {"sessions": [
        _entry("old.lua", "2026-01-01T00:00:00", "2026-09-01T10:00:00", [("a", "Eskuvy", 28731)], map_name="PersianGulf"),
        _entry("new.lua", "2026-01-01T00:00:00", "2026-11-03T10:00:00", [("a", "Eskuvy", 28731), ("b", "Pier_Paolo", 25245)],
               map_name="PersianGulf")]}
    newest, older = commands._campaign_blocks(history, players, count=0, latest_top=2, top=1)
    assert all(len(_visible(line)) < 78 for line in newest[1:] + older)
    assert older[0].count("..") == 1 and len(_visible(older[0])) < 78       # cut at the end of the rank
    assert " — **PILOTO POR" in older[0] and older[0].index("..") < older[0].index("28,731")
    # a short rank is left alone
    players["Eskuvy"]["custom_rank"] = "Ace"
    assert "**Ace**" in commands._campaign_blocks(history, players, count=0, latest_top=2, top=1)[1][0]


def test_a_pilots_own_medal_replaces_the_first_place_medal_when_only_the_champion_is_shown():
    players = {"Zarpa": {"ucid": "a", "credits": 5000, "custom_medal": "🦇"},
               "Viper": {"ucid": "b", "credits": 5000}}
    one_line = commands._campaign_blocks(HISTORY, players, count=0, latest_top=1, top=1)
    assert one_line[0][0].startswith("__") and " 🦇 `Zarpa`" in one_line[0][0] and "🥇" not in one_line[0][0]   # the newest
    assert " 🦇 `Zarpa`" in one_line[2][0] and " 🥇 `Viper`" in one_line[1][0]                                 # older ones, as before
    plain = {"Zarpa": {"ucid": "a", "credits": 5000}}
    assert " 🥇 `Zarpa`" in commands._campaign_blocks(HISTORY, plain, count=0, latest_top=1, top=1)[0][0]    # none: the medal
    several = commands._campaign_blocks(HISTORY, players, count=0, latest_top=3, top=1)
    assert several[0][1].startswith("🥇 `Zarpa`") and "🦇" not in "".join(several[0])                       # three places: medals
