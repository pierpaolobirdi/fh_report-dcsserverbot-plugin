"""The schema DCSServerBot's web console builds the settings form from (plugins/fh_report/schemas)."""
import os
import subprocess
import sys

import pytest
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import migrate_config as mc  # noqa: E402

SCHEMA = os.path.join(ROOT, "plugins", "fh_report", "schemas", "fh_report_schema.yaml")


def _element_keys() -> set:
    with open(SCHEMA, encoding="utf-8") as f:
        doc = yaml.safe_load(f)
    return set(doc["schema;element_schema"]["mapping"])


def test_every_known_variable_is_in_the_schema_and_nothing_else():
    keys = _element_keys() - {"disable_updates"}          # the old name, kept as "deprecated"
    assert keys == set(mc.KNOWN_VARS), (sorted(keys ^ set(mc.KNOWN_VARS)))


def test_report_layout_pattern_takes_letters_and_comma_separated_groups():
    import re
    with open(SCHEMA, encoding="utf-8") as f:
        pat = yaml.safe_load(f)["schema;element_schema"]["mapping"]["report_layout"]["pattern"]
    for ok in ("R", "DPSR", "RD", "DP, SR", "dp,sr", "DP,SR,R"):
        assert re.match(pat, ok), ok
    for bad in ("", "XYZ", "DPSRD", "DP,", ",R", "D P"):
        assert not re.match(pat, bad), bad


DCSSB = os.environ.get("DCSSB_DIR", "/home/user/special-k-s-flightsim-bots/dcsserverbot")
CHECK = r'''
import sys, types, os
tmp, dcssb, schema, *files = sys.argv[1:]
os.makedirs(tmp + "/core", exist_ok=True); os.makedirs(tmp + "/conf", exist_ok=True)
open(tmp + "/core/__init__.py", "w").close()
open(tmp + "/core/const.py", "w").write("DEFAULT_TAG = 'DEFAULT'")
open(tmp + "/core/commandline.py", "w").write("import types\nCOMMAND_LINE_ARGS = types.SimpleNamespace(config=%r, node='Master')" % (tmp + "/conf"))
open(tmp + "/conf/nodes.yaml", "w").write("Master:\n  instances:\n    DCS_Server: {}\n    DCS_Server_2: {}\nAgent:\n  instances:\n    DCS_Remote: {}\n")
open(tmp + "/conf/servers.yaml", "w").write("Srv1: {}\n")
sys.path.insert(0, tmp); os.chdir(dcssb)
from pykwalify.core import Core
from pykwalify.errors import PyKwalifyException
for f in files:
    c = Core(source_file=f, schema_files=[schema, dcssb + "/schemas/commands_schema.yaml"], file_encoding="utf-8",
             extensions=[dcssb + "/core/utils/validators.py"])
    try:
        c.validate(raise_exception=True); print(os.path.basename(f), "OK")
    except PyKwalifyException:
        print(os.path.basename(f), "INVALID")
'''


def _validate(tmp_path, **docs) -> dict:
    files = []
    for name, text in docs.items():
        p = tmp_path / f"{name}.yaml"
        p.write_text(text, encoding="utf-8")
        files.append(str(p))
    out = subprocess.run([sys.executable, "-c", CHECK, str(tmp_path / "h"), DCSSB, SCHEMA, *files],
                         capture_output=True, text=True, timeout=120)
    return dict(line.split()[:2] for line in out.stdout.splitlines() if line.endswith(("OK", "INVALID")))


@pytest.mark.skipif(not os.path.isdir(DCSSB) or subprocess.run([sys.executable, "-c", "import pykwalify, ruamel.yaml"],
                                                                capture_output=True).returncode != 0,
                    reason="needs pykwalify and a DCSServerBot checkout (DCSSB_DIR)")
def test_the_schema_accepts_our_config_forms_and_refuses_mistakes(tmp_path):
    example = open(os.path.join(ROOT, "config", "plugins", "fh_report.yaml"), encoding="utf-8").read()
    by_node = ("DEFAULT:\n  report_layout: \"DP, SR\"\n  update_interval: 120\nMaster:\n  DCS_Server:\n"
               "    channel_id: 123456789012345678\n    campaign_name: A\n    daily_reset_schedule: {sat: \"7:00\", sun: 7:30}\n"
               "    commands_channel_id: [111, 222]\n    admin: [Admin, Bob]\nAgent:\n  DCS_Remote:\n"
               "    channel_id: [123456789012345679]\n    commands_channel_id: \"111,222\"\n")
    got = _validate(tmp_path, example=example, by_node=by_node,
                    bad_layout="DEFAULT:\n  report_layout: XYZ\n",
                    bad_number="DEFAULT:\n  update_interval: fast\n",
                    unknown="DEFAULT:\n  bogus: 1\n")
    assert got == {"example.yaml": "OK", "by_node.yaml": "OK", "bad_layout.yaml": "INVALID",
                   "bad_number.yaml": "INVALID", "unknown.yaml": "INVALID"}
