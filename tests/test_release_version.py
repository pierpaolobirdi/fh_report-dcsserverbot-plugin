"""Where the plugin's version lives.

DCSServerBot's core reads __version__ (plugins/fh_report/version.py, via __init__.py) as the
DATABASE SCHEMA version: it must stay "major.minor" or the core fails / logs "migrated to
version ...". The release x.y.z is PLUGIN_RELEASE in the same file, which the core ignores.
"""
import io
import os
import re
import zipfile

import pytest

from conftest import ROOT, commands

PLUGIN_DIR = os.path.join(ROOT, "plugins", "fh_report")
TOP = "owner-fh_report-dcsserverbot-plugin-abc1234/"


def _read(*parts) -> str:
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
        return f.read()


def _repo_zip(mutate=None) -> bytes:
    """GitHub-style zip of the plugin's real files; `mutate` may edit {path: text}."""
    files = {name: _read(*name.split("/")) for name in commands.UPGRADE_FILES}
    if mutate:
        mutate(files)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, text in files.items():
            z.writestr(TOP + name, text)
    return buf.getvalue()


def _old_check(commands_py: str):
    """The version check of the plugins before 14.6.1, copied as it was: it read the release
    from commands.py. A plugin that old uses it on the zip it downloads."""
    m = re.search(rb'^FH_REPORT_RELEASE\s*=\s*"([^"]+)"', commands_py.encode(), re.MULTILINE)
    return m.group(1).decode() if m else None


def test_the_schema_version_stays_major_minor_for_the_core():
    from fh_report import __version__ as package_version
    from fh_report import version
    assert re.fullmatch(r"\d+\.\d+", version.__version__)            # the core does version.split('.') into two parts
    assert package_version == version.__version__                    # one source for what the core reads


def test_the_release_is_x_y_z_and_is_what_the_plugin_shows():
    from fh_report import version
    assert re.fullmatch(r"\d+\.\d+\.\d+", version.PLUGIN_RELEASE)
    assert commands.PLUGIN_RELEASE == version.PLUGIN_RELEASE
    assert commands._release_version(version.PLUGIN_RELEASE) is not None


def test_the_release_line_has_the_exact_shape_install_cmd_and_tools_search_for():
    lines = _read("plugins", "fh_report", "version.py").splitlines()
    starting = [ln for ln in lines if ln.startswith("PLUGIN_RELEASE")]
    assert len(starting) == 1 and re.fullmatch(r'PLUGIN_RELEASE = "\d+\.\d+\.\d+"', starting[0])


def test_install_cmd_reads_the_release_from_version_py():
    text = _read("install.cmd")
    assert 'findstr /B /C:"PLUGIN_RELEASE = " "%SCRIPT_DIR%plugins\\fh_report\\version.py"' in text
    assert "FH_REPORT_RELEASE" in text                               # only to read the version of an older installation
    new_ver_line = next(ln for ln in text.splitlines() if "set \"NEW_VER=%%~V\"" in ln)
    assert "version.py" in new_ver_line and "commands.py" not in new_ver_line


# ── the bridge: a plugin older than 14.6.1 must still be able to update to this one ──────────

def test_bridge_line_equals_the_release():
    """Remove this test, together with the FH_REPORT_RELEASE line, in the release after 14.6.1."""
    assert commands.FH_REPORT_RELEASE == commands.PLUGIN_RELEASE


def test_bridge_an_older_plugin_accepts_this_release_zip():
    files = commands._read_release_zip(_repo_zip(), commands.PLUGIN_RELEASE)
    assert _old_check(files["plugins/fh_report/commands.py"].decode()) == commands.PLUGIN_RELEASE


def test_the_bridge_line_is_not_used_by_the_code():
    code = [ln for ln in _read("plugins", "fh_report", "commands.py").splitlines()
            if "FH_REPORT_RELEASE" in ln and not ln.lstrip().startswith("#")]
    assert len(code) == 1 and code[0].startswith("FH_REPORT_RELEASE = ")      # only its own definition


# ── the new check, with and without the bridge line ─────────────────────────────────────────

def test_the_release_check_reads_version_py_and_ignores_commands_py():
    def drop_bridge(files):
        files["plugins/fh_report/commands.py"] = re.sub(r'(?m)^FH_REPORT_RELEASE = .*\n', "",
                                                        files["plugins/fh_report/commands.py"])
    zip_without_bridge = _repo_zip(drop_bridge)
    assert commands._read_release_zip(zip_without_bridge, commands.PLUGIN_RELEASE)     # the release after 14.6.1


def test_a_release_that_announces_another_version_is_refused():
    def lie(files):
        files["plugins/fh_report/version.py"] = files["plugins/fh_report/version.py"].replace(
            commands.PLUGIN_RELEASE, "99.0.0")
    with pytest.raises(commands.UpgradeError, match="does not match its tag"):
        commands._read_release_zip(_repo_zip(lie), commands.PLUGIN_RELEASE)


def test_a_release_with_the_version_only_in_commands_py_is_refused():
    def old_layout(files):
        files["plugins/fh_report/version.py"] = '__version__ = "1.0"\n'
    with pytest.raises(commands.UpgradeError, match="does not match its tag"):
        commands._read_release_zip(_repo_zip(old_layout), commands.PLUGIN_RELEASE)
