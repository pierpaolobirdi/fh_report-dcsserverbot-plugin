"""tools/fh_report_config_core.py: reading, checking and saving fh_report.yaml for the configuration editor."""
import importlib.util
import os
import sys

import pytest

pytest.importorskip("ruamel.yaml")

from conftest import ROOT  # noqa: E402

_spec = importlib.util.spec_from_file_location("fh_report_config_core", os.path.join(ROOT, "tools", "fh_report_config_core.py"))
core = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = core          # dataclasses look their module up here
_spec.loader.exec_module(core)

EXAMPLE = os.path.join(ROOT, "config", "plugins", "fh_report.yaml")
NODES = """DEFAULT:
  update_interval: 120
  show_new_release: true
Master:
  DCS.one:
    channel_id: 111
    campaign_name: "One"
  DCS.two:
    channel_id: 222
    campaign_name: Two
Agent:
  DCS.one:
    channel_id: 333
    campaign_name: Three
"""


@pytest.fixture(scope="module")
def model():
    return core.Model()


def _example():
    with open(EXAMPLE, encoding="utf-8") as f:
        return f.read()


def _file(text, model):
    return core.ConfigFile(text, model)


# ── where the file is ───────────────────────────────────────────────────────────────────────

def test_the_installation_is_found_from_its_folder_the_file_or_a_folder_inside(tmp_path, tmp_path_factory):
    cfg = tmp_path / "config" / "plugins"
    cfg.mkdir(parents=True)
    (cfg / "fh_report.yaml").write_text("DEFAULT:\n")
    (tmp_path / "plugins" / "fh_report" / "schemas").mkdir(parents=True)
    schema = tmp_path / "plugins" / "fh_report" / "schemas" / "fh_report_schema.yaml"
    schema.write_text("x: 1\n")
    for start in (tmp_path, cfg / "fh_report.yaml", cfg, tmp_path / "plugins" / "fh_report"):
        found = core.find_installation(str(start))
        assert found.root == str(tmp_path) and found.config_file == str(cfg / "fh_report.yaml")
        assert found.schema_file == str(schema)
    with pytest.raises(core.ConfigError):                                  # a folder with no installation above it
        core.find_installation(str(tmp_path_factory.mktemp("nothing")))
    with pytest.raises(core.ConfigError):
        core.find_installation(str(schema))                                 # a file, but not fh_report.yaml


# ── what each option is ─────────────────────────────────────────────────────────────────────

def test_every_catalog_option_gets_a_kind_a_section_and_a_plain_explanation(model):
    assert set(model.options) >= {"channel_id", "report_layout", "show_new_release"}
    kinds = {n: s.kind for n, s in model.options.items()}
    assert kinds["show_map"] == "bool" and kinds["update_interval"] == "int" and kinds["channel_id"] == "multi"
    assert kinds["excluded_ucids"] == "list" and kinds["daily_reset_schedule"] == "weekdays"
    assert kinds["report_layout"] == kinds["campaign_name"] == "text"
    assert model.options["daily_reset_schedule"].weekdays == ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
    assert model.options["zone_name_length"].min == 8 and model.options["zone_name_length"].max == 24
    for spec in model.options.values():
        assert spec.help and not spec.help.startswith("#") and not spec.help.startswith(spec.name + " "), spec.name
    assert [s["id"] for s in model.sections][0] == "server"
    assert len(sum((model.in_section(s["id"]) for s in model.sections), [])) == len(model.options)


def test_plain_help_removes_the_comment_marks_and_the_name_prefix():
    text = "#   max_zones        - Max zones shown per column, omit = all       (default: 15)\n#                      Second line\n#                        indented"
    assert core.plain_help(text, "max_zones") == "Max zones shown per column, omit = all       (default: 15)\nSecond line\n  indented"


# ── typing and checking values ─────────────────────────────────────────────────────────────

def test_typed_text_becomes_the_stored_value_or_a_clear_problem(model):
    o = model.options
    assert core.parse_input(o["channel_id"], "123") == (123, None)
    assert core.parse_input(o["channel_id"], "123, 456") == ([123, 456], None)
    assert core.parse_input(o["channel_id"], "12x")[1] == "must be a whole number, or several separated by commas"
    assert core.parse_input(o["commands_channel_id"], "111,222") == ([111, 222], None)
    assert core.parse_input(o["update_interval"], " 90 ") == (90, None)
    assert core.parse_input(o["update_interval"], "fast")[1] == "must be a whole number"
    assert core.parse_input(o["zone_name_length"], "30")[1] == "must be at most 24"
    assert core.parse_input(o["zone_name_length"], "3")[1] == "must be at least 8"
    assert core.parse_input(o["report_layout"], "DP, SR") == ("DP, SR", None)
    assert core.parse_input(o["report_layout"], "XYZ")[1].startswith("is not valid")
    assert core.parse_input(o["excluded_ucids"], "aaa\n bbb \n\n") == (["aaa", "bbb"], None)
    assert core.parse_input(o["excluded_ucids"], "aaa\naaa")[1] == "has a repeated entry"
    assert core.parse_input(o["show_map"], True) == (True, None)
    assert core.parse_input(o["daily_reset_schedule"], {"sat": "7:00", "sun": "  "}) == ({"sat": "7:00"}, None)
    assert core.parse_input(o["daily_reset_schedule"], {"xyz": "7:00"})[1].startswith("unknown day")
    for name in ("update_interval", "report_layout", "channel_id", "excluded_ucids", "daily_reset_schedule"):
        assert core.parse_input(o[name], "" if name != "daily_reset_schedule" else {})[0] is core.UNSET


def test_stored_values_are_shown_the_way_they_are_typed(model):
    o = model.options
    assert core.value_to_input(o["channel_id"], [1, 2]) == "1, 2"
    assert core.value_to_input(o["excluded_ucids"], ["a", "b"]) == "a\nb"
    assert core.value_to_input(o["show_map"], True) is True
    assert core.value_to_input(o["update_interval"], None) == ""
    assert core.value_to_input(o["daily_reset_schedule"], {"sat": "7:00"}) == {"sat": "7:00"}


def test_check_value_follows_the_schema_rules(model):
    o = model.options
    assert core.check_value(o["update_interval"], True) == "must be a whole number"
    assert core.check_value(o["show_map"], "yes") == "must be true or false"
    assert core.check_value(o["daily_reset_hour"], 5) == "must be text"            # the schema type is str
    assert core.check_value(o["admin"], ["Admin", "Bob"]) is None and core.check_value(o["admin"], 5) is not None
    assert core.check_value(o["saves_dir"], None) is None                          # an empty value is allowed there
    assert core.check_value(o["campaign_name"], None) == "needs a value, or remove the line"


# ── the file ──────────────────────────────────────────────────────────────────────────────────

def test_reading_and_writing_without_changes_gives_the_same_file_even_with_windows_line_ends(model):
    text = _example()
    assert _file(text, model).dumps() == text
    crlf = text.replace("\n", "\r\n")
    assert _file(crlf, model).dumps() == crlf


def test_flat_and_node_blocks_are_told_apart_and_values_say_where_they_come_from(model):
    cf = _file(NODES, model)
    assert [t.key for t in cf.targets()] == [("DEFAULT",), ("Master", "DCS.one"), ("Master", "DCS.two"), ("Agent", "DCS.one")]
    one, default = cf.targets()[1], cf.targets()[0]
    assert cf.get(one, "channel_id") == (111, "own")
    assert cf.get(one, "update_interval") == (120, "DEFAULT")
    assert cf.get(one, "bar_length") == (40, "default")
    assert cf.get(default, "update_interval") == (120, "own")
    flat = _file(_example(), model).targets()
    assert [t.key for t in flat] == [("DEFAULT",), ("DCS_Server",)]


def test_changes_touch_only_what_changed_and_keep_the_comments(model):
    text = _example()
    cf = _file(text, model)
    default, server = cf.targets()
    cf.set(default, "report_layout", "DPS")
    cf.set(server, "update_interval", 120)
    cf.set(server, "excluded_ucids", ["abc", "def"])
    cf.set(default, "show_map", core.UNSET)
    out = cf.dumps().split("\n")
    old = text.split("\n")
    assert [l for l in out if l not in old and l.strip()] == [
        "  report_layout: DPS # Letters D/P/S/R, any order/subset — see header",
        "  update_interval: 120", "  excluded_ucids:", "    - abc", "    - def"]
    assert [l for l in old if l not in out] == [
        "  report_layout: R  # Letters D/P/S/R, any order/subset — see header",
        "  show_map: true  # false = hide  |  true = show the map on the title's second line"]
    assert cf.dumps().startswith(text.split("DEFAULT:")[0])                 # the long header is untouched


def test_a_weekday_map_is_edited_in_place(model):
    cf = _file("DEFAULT:\n  daily_reset_hour: 5:00\n", model)
    default = cf.targets()[0]
    cf.set(default, "daily_reset_schedule", {"sat": "7:00", "sun": "7:30"})
    assert "  daily_reset_schedule:\n    sat: 7:00\n    sun: 7:30" in cf.dumps()
    cf.set(default, "daily_reset_schedule", {"sat": "8:00"})
    assert "    sat: 8:00" in cf.dumps() and "sun" not in cf.dumps()


def test_a_server_can_be_added_flat_or_under_a_node(model):
    cf = _file(NODES, model)
    flat = cf.add_server("DCS.new", 999, "New campaign")
    node = cf.add_server("DCS.third", 888, "Third", node="Agent")
    assert flat.key == ("DCS.new",) and node.key == ("Agent", "DCS.third")
    text = cf.dumps()
    assert "DCS.new:\n  channel_id: 999\n  campaign_name: New campaign" in text
    assert "  DCS.third:\n    channel_id: 888" in text
    assert _file(text, model).validate() == []
    with pytest.raises(core.ConfigError):
        cf.add_server("DCS.new", 1, "again")
    with pytest.raises(core.ConfigError):
        cf.add_server("bad: name", 1, "x")


def test_the_problems_found_are_listed_with_where_and_what(model):
    text = """DEFAULT:
  update_interval: fast
  zone_name_length: 30
  report_layout: XYZ
  disable_updates: false
  bogus: 1
DCS.a:
  channel_id: 5
  show_new_release: false
DCS.b:
  campaign_name: B
"""
    problems = {(p.where, p.option): p for p in _file(text, model).validate()}
    assert problems[("DEFAULT (every server)", "update_interval")].level == "error"
    assert problems[("DEFAULT (every server)", "zone_name_length")].message == "must be at most 24"
    assert problems[("DEFAULT (every server)", "report_layout")].message.startswith("is not valid")
    assert problems[("DEFAULT (every server)", "disable_updates")].level == "warning"
    assert "enable_updates" in problems[("DEFAULT (every server)", "disable_updates")].message
    assert problems[("DEFAULT (every server)", "bogus")].level == "warning"
    assert problems[("DCS.a", "show_new_release")].message == "is only read from DEFAULT"
    assert problems[("DCS.a", "campaign_name")].message.startswith("is missing")
    assert problems[("DCS.b", "channel_id")].message.startswith("is missing")


def test_the_example_config_has_no_problem(model):
    assert [str(p) for p in _file(_example(), model).validate()] == []


def test_a_key_written_twice_is_reported_with_its_line(model):
    text = "DEFAULT:\n  show_map: true\n  show_map: false\n"
    with pytest.raises(core.ConfigError) as err:
        _file(text, model)
    assert "line 3" in str(err.value) and "Delete one of the two lines" in str(err.value)
    with pytest.raises(core.ConfigError) as err:
        _file("DEFAULT:\n  a: [1, 2\n", model)
    assert "line" in str(err.value)


def test_saving_keeps_a_backup_writes_atomically_and_keeps_only_the_last_ten(model, tmp_path):
    path = tmp_path / "fh_report.yaml"
    path.write_text(_example(), encoding="utf-8")
    cf = core.ConfigFile.open(str(path), model)
    cf.set(cf.targets()[0], "report_layout", "DPS")
    backup = cf.save()
    assert backup and open(backup, encoding="utf-8").read() == _example()
    assert "report_layout: DPS" in path.read_text(encoding="utf-8")
    assert not [f for f in os.listdir(tmp_path) if f.endswith(".tmp")]
    assert core.ConfigFile.open(str(path), model).get(core.Target(("DEFAULT",)), "report_layout")[0] == "DPS"
    for i in range(14):                                                   # many saves: the folder does not fill up
        path.with_name(f"fh_report.yaml.2026010{i % 10}-0000{i:02d}.bak").write_text("old")
    cf.save()
    assert len([f for f in os.listdir(tmp_path) if f.endswith(".bak")]) == core.BACKUPS_KEPT
