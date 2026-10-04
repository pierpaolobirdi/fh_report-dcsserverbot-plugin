"""migrate_config.py, the yaml template and the plugin's option reads must stay in sync."""
import os
import pytest
import re
import subprocess
import sys

from conftest import ROOT, commands

sys.path.insert(0, ROOT)
import migrate_config as mc  # noqa: E402

YAML = os.path.join(ROOT, "config", "plugins", "fh_report.yaml")
COMMANDS = os.path.join(ROOT, "plugins", "fh_report", "commands.py")


def _default_block(text):
    block = re.search(r"^DEFAULT:\n((?:[ \t]+.*\n|#.*\n|\n)*)", text, re.M).group(1)
    return [m.group(1) for m in re.finditer(r"^  (\w+):", block, re.M)]


def _migrate(tmp_path, text):
    path = tmp_path / "fh_report.yaml"
    path.write_text(text, encoding="utf-8")
    out = subprocess.run([sys.executable, os.path.join(ROOT, "migrate_config.py"), str(path)],
                         capture_output=True, text=True, check=True).stdout
    return path.read_text(encoding="utf-8"), out


def test_every_default_is_known_and_commented():
    assert set(mc.DEFAULTS) <= mc.KNOWN_VARS
    assert set(mc.DEFAULTS) <= set(mc.COMMENTS)


def test_every_option_the_plugin_reads_is_known():
    code = open(COMMANDS, encoding="utf-8").read()
    used = set(re.findall(r'cfg\.get\(\s*"(\w+)"', code)) | {f"points_detail_{r}" for r in "DSR"}
    assert used <= mc.KNOWN_VARS, sorted(used - mc.KNOWN_VARS)


def test_yaml_template_header_matches_migrate_header():
    assert mc.HEADER_COMMENT.strip() in open(YAML, encoding="utf-8").read()


def test_yaml_default_block_follows_migrate_order():
    keys = [k for k in _default_block(open(YAML, encoding="utf-8").read()) if k in mc.DEFAULTS]
    assert keys == [k for k in mc.DEFAULTS if k in keys]


def test_show_map_sits_before_the_progress_bar_options():
    order = list(mc.DEFAULTS)
    assert mc.DEFAULTS["show_map"] is True
    assert order.index("daily_reset_hour") < order.index("show_map") < order.index("bar_length")
    assert "show_map" in mc.HEADER_COMMENT and "show_map" in open(YAML, encoding="utf-8").read()


def test_migration_adds_show_map_in_place_and_is_idempotent(tmp_path):
    old = open(YAML, encoding="utf-8").read().replace(
        "  show_map: true  # false = hide  |  true = show the map on the title's second line\n", "")
    assert "show_map: " not in old.split("DCS_Server:")[0]
    migrated, out = _migrate(tmp_path, old)
    assert "+ show_map: True" in out
    keys = _default_block(migrated)
    assert keys.index("daily_reset_hour") < keys.index("show_map") < keys.index("bar_length")
    assert re.search(r"^  show_map: true  # ", migrated, re.M)
    again, out2 = _migrate(tmp_path, migrated)
    assert again == migrated and "already up to date" in out2


def test_migration_keeps_a_user_choice_and_converts_0_1(tmp_path):
    base = open(YAML, encoding="utf-8").read()
    for written, expected in (("false", "false"), ("0", "false"), ("1", "true")):
        text = base.replace("  show_map: true  #", f"  show_map: {written}  #")
        migrated, _ = _migrate(tmp_path, text)
        assert re.search(rf"^  show_map: {expected}  # ", migrated, re.M), written


def test_server_block_override_is_not_flagged_obsolete(tmp_path):
    text = (open(YAML, encoding="utf-8").read()
            + '\n"MyInstance":\n  channel_id: 1\n  show_map: false\n  not_a_real_option: 1\n')
    migrated, out = _migrate(tmp_path, text)
    flagged = out.split("WARNING")[-1]
    assert "not_a_real_option" in flagged        # the check does look at this block...
    assert "show_map" not in flagged             # ...and show_map is a known option
    assert "  show_map: false" in migrated        # the override is left alone


@pytest.mark.parametrize("cfg, expected", [
    ({}, True),
    ({"enable_updates": True}, True),
    ({"enable_updates": False}, False),
    ({"enable_updates": "false"}, False),
    ({"disable_updates": True}, False),
    ({"disable_updates": False}, True),
    ({"enable_updates": True, "disable_updates": True}, True),
    ({"enable_updates": False, "disable_updates": False}, False),
])
def test_updates_enabled(cfg, expected):
    assert commands._updates_enabled(cfg) is expected


# ── daily reset times are HH:MM ────────────────────────────────────────────────

_OLD_TIMES = """DEFAULT:
  admin: Admin  # x
  daily_reset_hour: 4  # old: a plain hour
  daily_reset_schedule:
    sat: 7
    sun: 7  # weekend
  show_map: true  # x

"Public":
  channel_id: 1
  campaign_name: "C"
  daily_reset_hour: 6
  daily_reset_schedule:
    mon: 5
    fri: "22"
"""


def test_migration_turns_whole_hours_into_hh_mm_and_nothing_else(tmp_path):
    migrated, out = _migrate(tmp_path, _OLD_TIMES)
    assert re.search(r"^  daily_reset_hour: 4:00  #", migrated, re.M)
    assert "    sat: 7:00" in migrated and re.search(r"^    sun: 7:00  # weekend$", migrated, re.M)
    server = migrated.split('"Public":')[1]
    assert "  daily_reset_hour: 6:00" in server and "    mon: 5:00" in server and "    fri: 22:00" in server
    assert "4 → 4:00" in out
    again, _ = _migrate(tmp_path, migrated)
    assert again == migrated                                          # a second run changes nothing


def test_migration_leaves_hh_mm_values_alone(tmp_path):
    text = _OLD_TIMES.replace("daily_reset_hour: 4 ", "daily_reset_hour: 8:30 ").replace("sat: 7\n", "sat: 07:15\n")
    migrated, _ = _migrate(tmp_path, text)
    assert re.search(r"^  daily_reset_hour: 8:30  #", migrated, re.M) and "    sat: 07:15" in migrated


def test_migration_adds_a_missing_reset_time_as_0_00(tmp_path):
    text = _OLD_TIMES.replace("  daily_reset_hour: 4  # old: a plain hour\n", "")
    migrated, _ = _migrate(tmp_path, text)
    assert re.search(r"^  daily_reset_hour: 0:00  # ", migrated, re.M)


def test_the_commented_examples_in_the_template_use_the_new_format():
    text = open(YAML, encoding="utf-8").read()
    assert not re.search(r"^#?\s*(sat|sun|thu): \d{1,2}\s*$", text, re.M)


def test_show_bob_sits_next_to_show_punishment_in_header_and_default_block():
    assert "show_bob" in mc.DEFAULTS and mc.DEFAULTS["show_bob"] is True
    order = list(mc.DEFAULTS)
    assert order.index("show_bob") == order.index("show_punishment") + 1
    keys = _default_block(open(YAML, encoding="utf-8").read())
    assert keys.index("show_bob") == keys.index("show_punishment") + 1
    header = mc.HEADER_COMMENT
    assert header.index("#   show_punishment") < header.index("#   show_bob") < header.index("#   excluded_ucids")
    assert "Mission Statistics plugin of DCSServerBot" in header


def test_migration_adds_show_bob_as_true_and_keeps_a_user_choice(tmp_path):
    base = open(YAML, encoding="utf-8").read()
    old = re.sub(r"^  show_bob: .*\n", "", base, flags=re.M)
    assert not re.search(r"^  show_bob:", old, re.M)
    migrated, out = _migrate(tmp_path, old)
    assert re.search(r"^  show_bob: true  # ", migrated, re.M) and "+ show_bob: True" in out
    for written, expected in (("false", "false"), ("0", "false"), ("1", "true")):
        text = base.replace("  show_bob: true  #", f"  show_bob: {written}  #")
        again, _ = _migrate(tmp_path, text)
        assert re.search(rf"^  show_bob: {expected}  # ", again, re.M), written


# ── pilot limits are per table ─────────────────────────────────────────────────

def _limits_file(layout="R", extra="", server_layout=None, with_old=True):
    old = "  max_pilots: 20  # note\n  max_pilots_2t: 15\n  max_pilots_3t: 6\n" if with_old else ""
    server = f"  report_layout: {server_layout}\n" if server_layout else ""
    return (open(YAML, encoding="utf-8").read()
            .replace("  report_layout: R  #", f"  report_layout: {layout}  #", 1)
            .replace("  show_all_pilots: false  #", old + "  show_all_pilots: false  #", 1)
            .replace("DCS_Server:                             # instance name from nodes.yaml\n",
                     "DCS_Server:                             # instance name from nodes.yaml\n" + server + extra, 1))


def _live(text, section="DEFAULT"):
    block = text.split(f"{section}:")[1].split("\n\n")[0] if section == "DEFAULT" else text.split(f"{section}:")[1]
    return {m.group(1): m.group(2) for m in re.finditer(r"^[ \t]+(max_pilots\w*):[ \t]*(\d+)", block, re.M)}


@pytest.mark.parametrize("layout, expected", [
    ("R", {"max_pilots": "20"}),                          # one table: the old max_pilots, unchanged
    ("DS", {"max_pilots": "15"}),                         # two tables: what max_pilots_2t gave
    ("DSR", {"max_pilots": "6"}),                         # three tables: what max_pilots_3t gave
    ("DP, SR", {"max_pilots": "15", "max_pilots_D": "20"}),   # rotation: D alone had 20, S and R shared 15
])
def test_old_limits_become_per_table_limits_that_show_the_same_pilots(tmp_path, layout, expected):
    migrated, out = _migrate(tmp_path, _limits_file(layout))
    assert _live(migrated) == expected
    assert "max_pilots_2t" not in migrated and "max_pilots_3t" not in migrated
    assert "Pilot limits are now per table" in out


def test_a_server_with_another_layout_gets_explicit_limits_only_when_it_needs_them(tmp_path):
    same, _ = _migrate(tmp_path, _limits_file("DS", server_layout="DS"))
    assert _live(same, "DCS_Server") == {}                                    # it inherits the DEFAULT, same result
    other, _ = _migrate(tmp_path, _limits_file("DS", server_layout="DSR"))
    assert _live(other, "DCS_Server") == {"max_pilots": "6"}                   # 3 tables used 6, not the DEFAULT's 15
    rot, _ = _migrate(tmp_path, _limits_file("DP, SR", server_layout="DS"))
    assert _live(rot, "DCS_Server") == {"max_pilots_D": "15"}                 # S already inherits 15


def test_the_three_limits_end_up_together_in_order_before_show_all_pilots(tmp_path):
    migrated, _ = _migrate(tmp_path, _limits_file("DP, SR"))
    lines = [l.strip() for l in migrated.split("DEFAULT:")[1].split("\n\n")[0].splitlines()]
    at = [i for i, l in enumerate(lines) if re.match(r"#?\s*max_pilots", l)]
    assert [lines[i].lstrip("# ").split(":")[0] for i in at] == ["max_pilots", "max_pilots_R", "max_pilots_S", "max_pilots_D"]
    assert at == list(range(at[0], at[0] + 4)) and lines[at[-1] + 1].startswith("show_all_pilots")
    assert lines[at[0]].startswith("max_pilots:") and lines[at[1]].startswith("#")     # live kept, missing ones as examples


def test_a_config_without_the_old_two_and_three_table_keys_is_left_as_it_was(tmp_path):
    text = _limits_file("DSR", with_old=False).replace("  show_all_pilots: false  #", "  max_pilots: 12\n  show_all_pilots: false  #", 1)
    migrated, out = _migrate(tmp_path, text)
    assert _live(migrated) == {"max_pilots": "12"} and "Pilot limits are now per table" not in out


def test_the_pilot_limit_migration_is_repeatable(tmp_path):
    migrated, _ = _migrate(tmp_path, _limits_file("DP, SR", server_layout="DS"))
    again, out = _migrate(tmp_path, migrated)
    assert again == migrated and "Pilot limits are now per table" not in out


def test_the_new_limit_keys_are_known_and_the_old_ones_are_not():
    assert {"max_pilots", "max_pilots_R", "max_pilots_S", "max_pilots_D"} <= mc.KNOWN_VARS
    assert not {"max_pilots_2t", "max_pilots_3t"} & mc.KNOWN_VARS
