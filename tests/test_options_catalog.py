"""tools/fh_report_options.yaml: the one place where each option is described (sections, one line,
default, release it appeared in), and the schema it feeds."""
import importlib.util
import os
import re

import yaml

from conftest import ROOT

import migrate_config as mc

_spec = importlib.util.spec_from_file_location("sync_option_docs", os.path.join(ROOT, "tools", "sync_option_docs.py"))
sync = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sync)

REQUIRED_AND_PER_SERVER = {"channel_id", "campaign_name", "saves_dir", "commands_channel_id"}


def _catalog():
    with open(sync.CATALOG, encoding="utf-8") as f:
        return yaml.safe_load(f)


def test_the_catalog_has_exactly_the_options_the_plugin_knows():
    assert set(_catalog()["options"]) == set(mc.KNOWN_VARS) | REQUIRED_AND_PER_SERVER


def test_every_option_is_in_exactly_one_section_and_every_section_is_described():
    c = _catalog()
    listed = [o for s in c["sections"] for o in s["options"]]
    assert sorted(listed) == sorted(c["options"])
    assert all(s["id"] and s["title"] and s["intro"] for s in c["sections"])


def test_defaults_agree_with_the_ones_the_migration_writes():
    c = _catalog()["options"]
    for key, value in mc.DEFAULTS.items():
        assert c[key].get("default") == (value.strip('"') if isinstance(value, str) else value), key


def test_each_option_has_a_short_english_description_and_a_release_number():
    for name, opt in _catalog()["options"].items():
        assert 10 <= len(opt["desc"]) <= 130, name
        assert re.fullmatch(r"[\x20-\x7e]+", opt["desc"]), f"{name}: description must be plain English text"
        assert not any(ch in opt["desc"] for ch in "{}"), name
        assert re.fullmatch(r"\d+\.\d+\.\d+", opt["since"]), name


def test_the_schema_and_migrate_config_carry_the_catalog_descriptions():
    schema, migrate = sync.synced()
    assert schema == sync._read(sync.SCHEMA) and migrate == sync._read(sync.MIGRATE), \
        "run: python tools/sync_option_docs.py"
    with open(sync.SCHEMA, encoding="utf-8") as f:
        mapping = yaml.safe_load(f)["schema;element_schema"]["mapping"]
    for name, opt in _catalog()["options"].items():
        assert mapping[name]["desc"] == opt["desc"], name


def test_syncing_twice_changes_nothing_and_a_changed_description_is_replaced():
    text = sync._read(sync.SCHEMA)
    descs = {n: o["desc"] for n, o in _catalog()["options"].items()}
    assert sync.described_schema(text, descs) == text
    changed = sync.described_schema(text, {**descs, "show_map": "Another text", "excluded_ucids": "Other list"})
    assert changed.count("desc: 'Another text'") == 1 and "desc: 'Other list'" in changed
    assert descs["show_map"] not in changed and descs["excluded_ucids"] not in changed
