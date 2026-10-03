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
