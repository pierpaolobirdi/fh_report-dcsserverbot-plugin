"""session_close / session_delete / session_reopen / session_edit: what an admin does by hand to the history."""
import asyncio
import copy
import json
import os
import re

import pytest

from conftest import FileNode, commands, make_plugin
from test_campaign_history import HISTORY, _entry
from test_session import IDS

UTC = commands.timezone.utc
RUNNING = HISTORY["campaigns"][-1]                 # Syria, running
FINISHED = HISTORY["campaigns"][-2]                # Syria, ended 03-11-2026
OTHER = HISTORY["campaigns"][1]                    # Caucasus


# ── what is done to the history (pure) ─────────────────────────────────────────────────────────

def test_closing_by_hand_ends_the_running_campaign_and_makes_it_final():
    hist = copy.deepcopy(HISTORY)
    entry = commands.campaign_close_now(hist, "2026-11-10T09:00:00")
    assert not entry["open"] and entry["end"] == "2026-11-10T09:00:00" and entry["closed_by"] == "manual" and entry["final"]
    with pytest.raises(ValueError, match="no running session"):
        commands.campaign_close_now(hist, "2026-11-10T10:00:00")


def test_a_campaign_closed_by_hand_is_not_reopened_by_the_points_of_the_same_save():
    table = {"a": {"name": "Zarpa", "points": 7}}
    kwargs = dict(file="foothold_syria.lua", kind="detected", now="2026-11-10T09:00:00", map_name="Syria",
                  min_points=0, table=table)
    hist = copy.deepcopy(HISTORY)
    commands.campaign_close_now(hist, "2026-11-10T09:00:00")
    commands.campaign_history_update(hist, session="2026-11-10T09:00:00", **kwargs)
    assert len(hist["campaigns"]) == 5 and hist["campaigns"][-1]["open"]            # a new one
    auto = copy.deepcopy(HISTORY)
    auto["campaigns"][-1].update(open=False, end="2026-11-10T09:00:00")             # the same, closed by the plugin
    commands.campaign_history_update(auto, session="2026-11-10T09:00:00", **kwargs)
    assert len(auto["campaigns"]) == 4 and auto["campaigns"][-1]["open"]            # reopened, not duplicated


def test_only_a_finished_campaign_can_be_deleted():
    hist = copy.deepcopy(HISTORY)
    assert commands.campaign_remove(hist, OTHER["id"])["map"] == "Caucasus" and len(hist["campaigns"]) == 3
    with pytest.raises(ValueError, match="close it first"):
        commands.campaign_remove(hist, RUNNING["id"])
    with pytest.raises(ValueError, match="not in the history"):
        commands.campaign_remove(hist, "nope")


def test_reopening_follows_the_save_being_played_and_merges_the_running_one_in():
    hist = copy.deepcopy(HISTORY)
    hist["campaigns"][-1]["players"]["z"] = {"name": "New", "points": 40}             # the running one has a pilot more
    hist["campaigns"][-1]["players"]["a"] = {"name": "Zarpa", "points": 99999}        # and a higher score
    entry, dropped = commands.campaign_reopen(hist, FINISHED["id"], "foothold_syria.lua", "2026-11-03T10:00:00")
    assert entry["open"] and entry["end"] is None and entry["session"] == "2026-11-03T10:00:00"
    assert dropped["id"] == RUNNING["id"] and len(hist["campaigns"]) == 3
    assert entry["players"]["a"]["points"] == 99999 and entry["players"]["z"]["points"] == 40 \
        and entry["players"]["b"]["points"] == 30110
    assert entry["total"] == sum(p["points"] for p in entry["players"].values()) and "final" not in entry
    with pytest.raises(ValueError, match="already the one running"):
        commands.campaign_reopen(hist, FINISHED["id"], "foothold_syria.lua", "x")
    with pytest.raises(ValueError, match="own map is running"):
        commands.campaign_reopen(copy.deepcopy(HISTORY), OTHER["id"], "foothold_syria.lua", "x")


def test_editing_corrects_the_map_and_the_dates_and_an_exact_start_loses_its_tilde():
    hist = copy.deepcopy(HISTORY)
    first = hist["campaigns"][0]                                                      # start is an estimate
    entry, changes = commands.campaign_edit(hist, first["id"], map_name="sinaimap", start="2026-08-01T00:00:00",
                                            end="2026-09-21T10:00:00")
    assert changes == ["map SYRIA → SINAI", "start date", "end date"]
    assert (entry["map"], entry["start"], entry["end"], entry["start_kind"]) == (
        "SINAI", "2026-08-01T00:00:00", "2026-09-21T10:00:00", "detected")
    assert commands.campaign_edit(hist, first["id"], map_name="SINAI")[1] == []        # nothing new
    with pytest.raises(ValueError, match="before the start"):
        commands.campaign_edit(hist, first["id"], end="2026-07-01T00:00:00")
    with pytest.raises(ValueError, match="no end yet"):
        commands.campaign_edit(hist, RUNNING["id"], end="2026-12-01T00:00:00")
    assert commands.campaign_edit(hist, RUNNING["id"], map_name="Kola")[1] == ["map SYRIA → KOLA"]


# ── the commands ───────────────────────────────────────────────────────────────────────────────

class _Followup:
    def __init__(self):
        self.sent = []

    async def send(self, content=None, embed=None, view=None, ephemeral=None):
        self.sent.append((content, view, ephemeral))
        return object()


class _Response:
    def __init__(self):
        self.deferred, self.edits = None, []

    async def defer(self, ephemeral=None):
        self.deferred = ephemeral

    async def edit_message(self, **kw):
        self.edits.append(kw)


def _setup(tmp_path, history=HISTORY, admin=True, state=True):
    d = str(tmp_path)
    os.makedirs(os.path.join(d, ".fhc"), exist_ok=True)
    with open(os.path.join(d, ".fhc", "fhr_session_history.json"), "w") as f:
        json.dump(history, f)
    session = {"file": "foothold_syria.lua", "start": "2026-11-03T10:00:00", "kind": "detected",
               "seen": "2026-11-04T08:00:00", "base": {"points": {}, "kills": {}}}
    with open(os.path.join(d, ".fhc", "fhr_session.json"), "w") as f:
        json.dump(session, f)
    srv = type("S", (), {"node": FileNode(), "status": commands.Status.RUNNING, "name": "Public",
                         "instance": type("N", (), {"name": "inst"})()})()
    audited = []

    async def audit(text, user=None):
        audited.append(text)
    plugin = make_plugin(bot=type("B", (), {"audit": staticmethod(audit)})())
    plugin._resolve_server = lambda interaction, param: ("inst", None)
    plugin._server_by_key = lambda key: srv
    plugin._is_admin = lambda interaction, key: admin
    plugin._merged_cfg = lambda key: {"saves_dir": d}
    plugin._public_server_name = lambda key: "Public"
    plugin._reset_tz = lambda server: UTC
    if state:
        plugin._session_state[d] = dict(session)
    interaction = type("I", (), {"followup": _Followup(), "response": _Response(),
                                 "user": type("U", (), {"id": 42})()})()
    return plugin, interaction, d, audited


def _stored(d):
    with open(os.path.join(d, ".fhc", "fhr_session_history.json")) as f:
        return json.load(f)["campaigns"]


def _press(view, button, user_id=42):
    inter = type("I", (), {"user": type("U", (), {"id": user_id})(), "response": _Response()})()
    assert asyncio.run(view.interaction_check(inter)) is (user_id == 42)
    asyncio.run(getattr(view, button)(inter, None))
    return inter.response.edits


def test_the_commands_are_for_admins_and_private(tmp_path):
    for name, kw in (("session_close", {}), ("session_delete", {"session": FINISHED["id"]}),
                     ("session_reopen", {"session": FINISHED["id"]}), ("session_edit", {"session": FINISHED["id"], "map_name": "Kola"})):
        plugin, inter, d, _ = _setup(tmp_path, admin=False)
        asyncio.run(getattr(plugin, name)(inter, **kw))
        assert inter.response.deferred is True and inter.followup.sent[0][0].startswith("❌ Only admins")
        assert _stored(d) == HISTORY["campaigns"]


def test_close_asks_first_and_only_the_one_who_asked_can_confirm(tmp_path):
    plugin, inter, d, audited = _setup(tmp_path)
    asyncio.run(plugin.session_close(inter))
    [(question, view, ephemeral)] = inter.followup.sent
    assert ephemeral is True and "SYRIA" in question and "starts another session from now" in question
    assert _stored(d) == HISTORY["campaigns"]                                          # nothing yet
    assert asyncio.run(view.interaction_check(type("I", (), {"user": type("U", (), {"id": 7})()})())) is False
    [edit] = _press(view, "cancel")
    assert "nothing was changed" in edit["content"] and _stored(d) == HISTORY["campaigns"] and not audited


def test_confirming_the_close_ends_the_campaign_and_starts_a_new_session(tmp_path):
    plugin, inter, d, audited = _setup(tmp_path)
    asyncio.run(plugin.session_close(inter))
    [edit] = _press(inter.followup.sent[0][1], "confirm")
    assert edit["content"].startswith("✅ Closed") and len(audited) == 1
    entry = _stored(d)[-1]
    assert not entry["open"] and entry["closed_by"] == "manual" and entry["final"] and entry["end"] > "2026-11-04"
    with open(os.path.join(d, ".fhc", "fhr_session.json")) as f:
        session = json.load(f)
    assert session["start"] == entry["end"] and session["kind"] == "detected" and session["base"] == {"points": {}, "kills": {}}
    assert plugin._session_state[d]["start"] == entry["end"]


def test_there_is_nothing_to_close_without_a_running_campaign(tmp_path):
    plugin, inter, d, _ = _setup(tmp_path, history={"campaigns": HISTORY["campaigns"][:3]})
    asyncio.run(plugin.session_close(inter))
    assert inter.followup.sent[0][0].startswith("There is no running session") and inter.followup.sent[0][1] is None


def test_delete_shows_what_goes_and_removes_it_on_confirmation(tmp_path):
    plugin, inter, d, audited = _setup(tmp_path)
    asyncio.run(plugin.session_delete(inter, session=OTHER["id"]))
    question, view, _ = inter.followup.sent[0]
    assert "CAUCASUS" in question and "Viper" in question and "cannot be undone" in question
    assert len(_stored(d)) == 4
    [edit] = _press(view, "confirm")
    assert edit["content"].startswith("✅ Deleted") and [c["id"] for c in _stored(d)] == [
        c["id"] for c in HISTORY["campaigns"] if c["id"] != OTHER["id"]] and len(audited) == 1


def test_delete_refuses_the_running_campaign_and_unknown_ones(tmp_path):
    for cid, words in ((RUNNING["id"], "close it first"), ("nope", "not in the history")):
        plugin, inter, d, _ = _setup(tmp_path)
        asyncio.run(plugin.session_delete(inter, session=cid))
        assert words in inter.followup.sent[0][0] and inter.followup.sent[0][1] is None and len(_stored(d)) == 4


def test_reopen_explains_what_happens_and_does_it_on_confirmation(tmp_path):
    plugin, inter, d, audited = _setup(tmp_path)
    asyncio.run(plugin.session_reopen(inter, session=FINISHED["id"]))
    question, view, _ = inter.followup.sent[0]
    assert "is dropped and its points are merged" in question and "SYRIA" in question
    [edit] = _press(view, "confirm")
    stored = _stored(d)
    assert edit["content"].startswith("✅ Reopened") and len(stored) == 3 and stored[-1]["open"]
    assert stored[-1]["id"] == FINISHED["id"] and stored[-1]["session"] == "2026-11-03T10:00:00" and len(audited) == 1


def test_reopen_needs_the_save_of_that_campaign_to_be_the_one_played(tmp_path):
    plugin, inter, d, _ = _setup(tmp_path)
    asyncio.run(plugin.session_reopen(inter, session=OTHER["id"]))
    assert "own map is running" in inter.followup.sent[0][0] and inter.followup.sent[0][1] is None
    plugin, inter, d, _ = _setup(tmp_path, state=False)
    asyncio.run(plugin.session_reopen(inter, session=FINISHED["id"]))
    assert "has not looked at this server's session yet" in inter.followup.sent[0][0]


def test_edit_applies_at_once_and_says_what_changed(tmp_path):
    plugin, inter, d, audited = _setup(tmp_path)
    asyncio.run(plugin.session_edit(inter, session=OTHER["id"], map_name="Kola", start="01-09-2026", end="12-10-2026"))
    [(text, _v, ephemeral)] = inter.followup.sent
    assert text.startswith("✅ Corrected") and "map CAUCASUS → KOLA, start date" in text and ephemeral is True
    edited = next(c for c in _stored(d) if c["id"] == OTHER["id"])
    assert edited["map"] == "KOLA" and edited["start"] == "2026-09-01T00:00:00" and edited["end"] == "2026-10-12T12:00:00"
    assert edited["start_kind"] == "detected" and len(audited) == 1


def test_edit_explains_what_it_cannot_do(tmp_path):
    cases = [({}, "Say what to correct"), ({"start": "31-02-2026"}, "couldn't read `31-02-2026`"),
             ({"end": "01-01-2020"}, "before the start"), ({"map_name": "Syria", "session": "nope"}, "not in the history"),
             ({"map_name": "SYRIA"}, "Nothing to change")]
    for kw, words in cases:
        plugin, inter, d, _ = _setup(tmp_path)
        asyncio.run(plugin.session_edit(inter, **{"session": FINISHED["id"], **kw}))
        assert words in inter.followup.sent[0][0], (kw, inter.followup.sent[0][0])
        assert _stored(d) == HISTORY["campaigns"]
    plugin, inter, d, _ = _setup(tmp_path)
    asyncio.run(plugin.session_edit(inter, session=RUNNING["id"], end="01-12-2026"))
    assert "no end yet" in inter.followup.sent[0][0]


def test_every_text_of_the_session_commands_fits_discords_100_characters():
    src = open(commands.__file__, encoding="utf-8").read()
    texts = []
    for m in re.finditer(r'@fh_report\.command\(name="session_\w+".*?\n    async def', src, re.S):
        texts += re.findall(r'(?:description|\w+)="([^"]+)"', m.group(0))
    for m in re.finditer(r'@app_commands\.describe\((.*?)\)\n', src, re.S):
        if "session=" in m.group(1):
            texts += re.findall(r'\w+="([^"]+)"', m.group(1))
    assert texts and all(len(t) <= 100 for t in texts), [t for t in texts if len(t) > 100]


def test_every_autocomplete_callback_takes_exactly_self_interaction_current():
    """discord.py refuses (TypeError when the plugin is loaded, so no slash command at all) an autocomplete
    callback with any other parameter; the stand-in of discord used by the tests does not check it."""
    import inspect
    found = [name for name in dir(commands.Fh_Report) if name.startswith("_autocomplete_")]
    assert len(found) >= 5
    for name in found:
        func = getattr(commands.Fh_Report, name)
        assert inspect.iscoroutinefunction(func), name
        assert list(inspect.signature(func).parameters) == ["self", "interaction", "current"], name


def test_the_history_is_read_from_the_file_it_had_before_until_the_new_one_exists(tmp_path):
    d = str(tmp_path)
    os.makedirs(os.path.join(d, ".fhc"))
    node = FileNode()
    old = os.path.join(d, ".fhc", "fhr_campaign_history.json")
    new = os.path.join(d, ".fhc", "fhr_session_history.json")
    assert asyncio.run(commands._read_session_history(node, d)) == {}
    with open(old, "w") as f:
        json.dump({"campaigns": [{"id": "old"}]}, f)
    assert asyncio.run(commands._read_session_history(node, d)) == {"campaigns": [{"id": "old"}]}
    with open(new, "w") as f:
        json.dump({"campaigns": [{"id": "new"}]}, f)
    assert asyncio.run(commands._read_session_history(node, d)) == {"campaigns": [{"id": "new"}]}
