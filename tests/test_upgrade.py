"""/fh_report upgrade: release choice, signature, zip checks, install with rollback, the flow."""
import asyncio
import io
import os
import zipfile

import pytest

from conftest import commands, make_plugin

TOP = "pierpaolobirdi-fh_report-dcsserverbot-plugin-abc1234/"      # GitHub's zip puts everything in one folder


def _zip(version="14.9.0", extra=None, drop=None, commands_src=None, top=TOP):
    src = commands_src or f'FH_REPORT_RELEASE = "{version}"\n'
    files = {n: "x = 1\n" for n in commands.UPGRADE_FILES}
    files["plugins/fh_report/commands.py"] = src
    files["migrate_config.py"] = "print('migrated')\n"
    files["README.md"] = "the rest of the repository\n"
    files["tests/test_x.py"] = "def test(): pass\n"
    files.update(extra or {})
    for n in drop or []:
        files.pop(n)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for n, c in files.items():
            z.writestr(top + n, c)
    return buf.getvalue()


def _rel(tag, pre=False, draft=False, zipball=True):
    return {"tag_name": tag, "prerelease": pre, "draft": draft, "body": f"notes {tag}",
            "published_at": "2026-10-10T00:00:00Z",
            "zipball_url": f"https://api.github.com/repos/x/zipball/{tag}" if zipball else None}


# ── choosing the release ─────────────────────────────────────────────────────────

def test_version_is_read_from_the_tag():
    assert commands._release_version("v.14.4.2") == (14, 4, 2) and commands._release_version("14.10.0") == (14, 10, 0)
    assert commands._release_version("latest") is None


def test_the_newest_release_above_the_installed_one_is_chosen():
    cur = (14, 4, 3)
    rels = [_rel("v.14.4.3"), _rel("v.14.5.0"), _rel("v.14.10.0"), _rel("v.14.6.0")]
    assert commands._pick_release(rels, cur)["text"] == "14.10.0"            # 14.10 is above 14.6 (not a string compare)
    assert commands._pick_release([_rel("v.14.4.3"), _rel("v.14.3.0")], cur) is None   # equal or older: nothing


def test_drafts_and_releases_without_a_zip_are_skipped():
    cur = (14, 4, 3)
    assert commands._pick_release([_rel("v.14.9.0", draft=True)], cur) is None
    assert commands._pick_release([_rel("v.14.9.0", zipball=False)], cur) is None
    assert commands._pick_release([_rel("v.14.9.0")], cur)["zip_url"].endswith("/zipball/v.14.9.0")


def test_a_prerelease_is_offered_and_flagged():
    got = commands._pick_release([_rel("v.14.9.0", pre=True), _rel("v.14.8.0")], (14, 4, 3))
    assert got["text"] == "14.9.0" and got["prerelease"] is True
    embed = commands._upgrade_embed("14.4.3", got)
    assert any("PRE-RELEASE" in f.name for f in embed.fields)
    stable = commands._pick_release([_rel("v.14.8.0")], (14, 4, 3))
    assert not any("PRE-RELEASE" in f.name for f in commands._upgrade_embed("14.4.3", stable).fields)


# ── the zip ──────────────────────────────────────────────────────────────────────

def test_only_the_expected_files_are_taken_from_the_whole_repository_zip():
    assert set(commands._read_release_zip(_zip(), "14.9.0")) == set(commands.UPGRADE_FILES)   # README, tests ignored


def test_a_zip_that_is_wrong_is_refused():
    for bad in (_zip(drop=["migrate_config.py"]), _zip(drop=["plugins/fh_report/version.py"]), b"not a zip"):
        with pytest.raises(commands.UpgradeError):
            commands._read_release_zip(bad, "14.9.0")
    with pytest.raises(commands.UpgradeError, match="layout"):                # two top folders: not GitHub's zip
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("a/plugins/fh_report/commands.py", "x")
            z.writestr("b/plugins/fh_report/commands.py", "x")
        commands._read_release_zip(buf.getvalue(), "14.9.0")
    with pytest.raises(commands.UpgradeError, match="compile"):
        commands._read_release_zip(_zip(extra={"plugins/fh_report/listener.py": "def (:\n"}), "14.9.0")
    with pytest.raises(commands.UpgradeError, match="version"):
        commands._read_release_zip(_zip(version="14.8.0"), "14.9.0")


# ── installing ───────────────────────────────────────────────────────────────────

def _folder(tmp_path):
    d = tmp_path / "fh_report"
    d.mkdir()
    for n in ("__init__.py", "commands.py", "listener.py", "version.py"):
        (d / n).write_text("old " + n)
    (d / "something_else.txt").write_text("keep me")
    return d


def test_install_replaces_the_files_keeps_a_backup_and_leaves_other_files(tmp_path):
    d = _folder(tmp_path)
    files = commands._read_release_zip(_zip(), "14.9.0")
    backup = commands._install_release_files(str(d), files)
    assert "14.9.0" in (d / "commands.py").read_text() and (d / "something_else.txt").read_text() == "keep me"
    assert open(os.path.join(backup, "commands.py")).read() == "old commands.py"
    assert not list(d.glob("*.new"))


def test_a_failure_halfway_puts_the_old_files_back(tmp_path, monkeypatch):
    d = _folder(tmp_path)
    files = commands._read_release_zip(_zip(), "14.9.0")
    real = os.replace
    calls = []

    def flaky(src, dst):
        calls.append(dst)
        if len(calls) == 3:
            raise OSError("disk full")
        real(src, dst)
    monkeypatch.setattr(commands.os, "replace", flaky)
    with pytest.raises(OSError):
        commands._install_release_files(str(d), files)
    assert all((d / n).read_text() == "old " + n for n in ("__init__.py", "commands.py", "listener.py", "version.py"))
    assert not list(d.glob("*.new"))


# ── the command flow ─────────────────────────────────────────────────────────────

class _Followup:
    def __init__(self):
        self.sent = []

    async def send(self, content=None, embed=None, view=None, ephemeral=None):
        self.sent.append((content, embed, view))


class _Interaction:
    def __init__(self):
        self.followup, self.edits = _Followup(), []
        self.user = type("U", (), {"id": 7})()
        self.response = type("R", (), {"defer": staticmethod(self._defer)})()

    async def _defer(self, ephemeral=None):
        pass

    async def edit_original_response(self, content=None, embed=None, view=None):
        self.edits.append(content)


def _plugin(releases, blobs, tmp_path, monkeypatch):
    monkeypatch.setattr(commands, "FH_REPORT_RELEASE", "14.4.3")
    plugin = make_plugin(node=type("N", (), {"config_dir": str(tmp_path / "config")})())
    restarted = []

    async def restart():
        restarted.append(True)
    plugin.bot = type("B", (), {"node": type("N", (), {"restart": staticmethod(restart)})()})()

    async def http(url, as_json=False):
        if as_json:
            return releases
        return blobs[url]
    plugin._http_get = http
    return plugin, restarted


def _run_command(plugin):
    interaction = _Interaction()
    asyncio.run(commands.Fh_Report.upgrade(plugin, interaction))
    return interaction


def test_up_to_date_says_so(tmp_path, monkeypatch):
    plugin, _ = _plugin([_rel("v.14.4.3")], {}, tmp_path, monkeypatch)
    assert "newest release" in _run_command(plugin).followup.sent[0][0]


def test_a_new_release_is_offered_with_buttons_for_the_admin_only(tmp_path, monkeypatch):
    plugin, _ = _plugin([_rel("v.14.9.0", pre=True)], {}, tmp_path, monkeypatch)
    content, embed, view = _run_command(plugin).followup.sent[0]
    assert content is None and any("14.9.0" in f.value for f in embed.fields)
    assert asyncio.run(view.interaction_check(type("I", (), {"user": type("U", (), {"id": 7})()})())) is True
    assert asyncio.run(view.interaction_check(type("I", (), {"user": type("U", (), {"id": 8})()})())) is False


def _confirm(plugin, release):
    interaction = _Interaction()
    asyncio.run(plugin._upgrade_run(interaction, release))
    return interaction


def test_confirming_installs_migrates_and_restarts(tmp_path, monkeypatch):
    d = _folder(tmp_path)
    monkeypatch.setattr(commands, "__file__", str(d / "commands.py"))
    data = _zip()
    rel = commands._pick_release([_rel("v.14.9.0")], (14, 4, 3))
    plugin, restarted = _plugin([], {rel["zip_url"]: data}, tmp_path, monkeypatch)
    plugin._run_migration = lambda src: ""
    interaction = _confirm(plugin, rel)
    assert "14.9.0" in (d / "commands.py").read_text() and restarted == [True]
    assert any("Restarting" in (e or "") for e in interaction.edits)


def test_a_broken_release_installs_nothing_and_does_not_restart(tmp_path, monkeypatch):
    d = _folder(tmp_path)
    monkeypatch.setattr(commands, "__file__", str(d / "commands.py"))
    rel = commands._pick_release([_rel("v.14.9.0")], (14, 4, 3))
    plugin, restarted = _plugin([], {rel["zip_url"]: _zip(version="14.8.0")}, tmp_path, monkeypatch)   # tag says 14.9.0
    interaction = _confirm(plugin, rel)
    assert (d / "commands.py").read_text() == "old commands.py" and restarted == []
    assert "does not match its tag" in interaction.edits[-1]


