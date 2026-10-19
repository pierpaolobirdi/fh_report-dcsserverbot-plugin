"""/fh_report podium: typing dates, defaults, the 6000-character embed limit and paging."""
import asyncio
import json
import os
from datetime import date, datetime, timedelta, timezone

import pytest

from conftest import FileNode, commands, make_plugin

TODAY = date(2026, 10, 19)


@pytest.mark.parametrize("text, expected", [
    ("25-12-2026", date(2026, 12, 25)), ("25/12/2026", date(2026, 12, 25)), ("25.12.2026", date(2026, 12, 25)),
    ("25122026", date(2026, 12, 25)), ("5-3-2026", date(2026, 3, 5)), ("25-12-26", date(2026, 12, 25)),
    ("25-12", date(2026, 12, 25)), ("2026-12-25", date(2026, 12, 25)), (" 25-12-2026 ", date(2026, 12, 25)),
    ("today", TODAY), ("Today", TODAY), ("yesterday", TODAY - timedelta(days=1)),
    ("7d", TODAY - timedelta(days=7)), ("0d", TODAY),
    ("", None), (None, None), ("31-02-2026", None), ("tomorrow", None), ("2026", None), ("25-13-2026", None),
])
def test_dates_are_read_in_the_usual_ways(text, expected):
    assert commands._parse_user_date(text, TODAY) == expected


def test_dates_are_shown_as_dd_mm_yyyy():
    assert commands._fmt_date(date(2026, 3, 5)) == "05-03-2026"


def test_blocks_are_packed_into_pages_without_splitting_a_day():
    day = ["__date__"] + ["🥇 `Name` — **Rank** — 1,000 pts"] * 50
    pages = commands._podium_pages([day] * 5, limit=3800)
    assert sum(p.count("__date__") for p in pages) == 5 and len(pages) == 3        # two days fit a page, never half a day
    assert all(len(p) <= 3800 for p in pages)
    small = [["__d__", "line"]] * 10
    assert len(commands._podium_pages(small)) == 1
    assert commands._podium_pages([]) == []


# ── the command ───────────────────────────────────────────────────────────────────────────

class _Followup:
    def __init__(self):
        self.sent = []

    async def send(self, content=None, embed=None, view=None, ephemeral=None):
        self.sent.append((content, embed, view))
        return object()


def _history(days, top=50):
    start = date(2026, 9, 1)
    return {(start + timedelta(days=i)).isoformat():
            [{"campaign_restart": False,
              "top": [{"name": f"Pilot{n}", "points": 1000 - n} for n in range(top)]}] for i in range(days)}


def _run(saves_dir, history, **kw):
    d = saves_dir()
    os.makedirs(os.path.join(d, ".fhc"), exist_ok=True)
    with open(os.path.join(d, ".fhc", "fhr_daily_history.json"), "w") as f:
        json.dump(history, f)
    followup = _Followup()
    srv = type("S", (), {"node": FileNode(), "status": commands.Status.RUNNING, "name": "Public"})()
    plugin = make_plugin()

    async def ctx(interaction, server_param):
        return "inst", srv, {"saves_dir": d}, d, commands._UpdateReadCache(srv.node), True
    plugin._command_context = ctx
    plugin._public_server_name = lambda name: "Public"
    plugin._reset_tz = lambda server: timezone.utc
    plugin._where_tag = lambda server: None
    interaction = type("I", (), {"followup": followup, "user": type("U", (), {"id": 42})()})()
    asyncio.run(plugin.podium(interaction, **kw))
    return followup.sent


def test_a_long_range_is_paged_and_every_page_fits_discords_6000_limit(saves_dir):
    sent = _run(saves_dir, _history(30), date_from="01-09-2026", date_to="30-09-2026", top=50)
    assert len(sent) == 1 and sent[0][2] is not None                 # one message, with the paging view
    view = sent[0][2]
    assert len(view.embeds) > 5
    assert all(commands._embed_size(e) <= 6000 for e in view.embeds)
    assert view.embeds[0].description == "01-09-2026 → 30-09-2026 · page 1/%d" % len(view.embeds)
    assert view.previous.disabled and not view.next.disabled


def test_the_paging_buttons_move_between_pages(saves_dir):
    view = _run(saves_dir, _history(30), date_from="01-09-2026", date_to="30-09-2026", top=50)[0][2]
    shown = []

    class R:
        async def edit_message(self, embed=None, view=None):
            shown.append(embed)
    inter = type("I", (), {"user": type("U", (), {"id": 42})(), "response": R()})()
    asyncio.run(view.next(inter, None))
    assert shown[-1] is view.embeds[1] and not view.previous.disabled
    asyncio.run(view.previous(inter, None))
    assert shown[-1] is view.embeds[0] and view.previous.disabled
    other = type("I", (), {"user": type("U", (), {"id": 7})()})()
    assert asyncio.run(view.interaction_check(other)) is False and asyncio.run(view.interaction_check(inter)) is True


def test_a_short_answer_is_one_embed_without_buttons(saves_dir):
    sent = _run(saves_dir, _history(3, top=5), date_from="01-09-2026", date_to="03-09-2026", top=3)
    content, embed, view = sent[0]
    assert view is None and embed.description == "01-09-2026 → 03-09-2026"


def test_one_day_is_shown_as_a_single_date_and_dates_typed_backwards_are_swapped(saves_dir):
    sent = _run(saves_dir, _history(3, top=5), date_from="02-09-2026", date_to="02-09-2026")
    assert sent[0][1].description == "02-09-2026"
    sent = _run(saves_dir, _history(3, top=5), date_from="03-09-2026", date_to="01-09-2026")
    assert sent[0][1].description == "01-09-2026 → 03-09-2026"


def test_nothing_typed_means_today_and_says_what_history_exists(saves_dir):
    sent = _run(saves_dir, _history(3, top=5))
    text = sent[0][0]
    assert "No history found" in text and "01-09-2026" in text and "03-09-2026" in text
    assert "today is not there yet" in text


def test_top_defaults_to_three_positions(saves_dir):
    sent = _run(saves_dir, _history(1, top=10), date_from="01-09-2026")
    assert "Top 3" in sent[0][1].title                                     # only a start date: up to today, top 3
    sent = _run(saves_dir, _history(1, top=10), date_from="01-09-2026", date_to="01-09-2026")
    body = "\n".join(f.value for f in sent[0][1].fields)
    assert "Pilot2" in body and "Pilot3" not in body


def test_an_unreadable_date_gets_a_message_that_shows_the_formats(saves_dir):
    sent = _run(saves_dir, _history(1), date_from="soon")
    assert "dd-mm-yyyy" in sent[0][0] and "`soon`" in sent[0][0] and "today" in sent[0][0]


def test_an_empty_history_says_so(saves_dir):
    sent = _run(saves_dir, {}, date_from="01-09-2026", date_to="03-09-2026")
    assert "Nothing has been recorded yet" in sent[0][0]
