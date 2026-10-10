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
    embed = commands._upgrade_embed("14.4.3", got, None, [])
    assert any("PRE-RELEASE" in f.name for f in embed.fields)
    stable = commands._pick_release([_rel("v.14.8.0")], (14, 4, 3))
    assert not any("PRE-RELEASE" in f.name for f in commands._upgrade_embed("14.4.3", stable, None, []).fields)


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


class _Response:
    def __init__(self, log):
        self.log = log

    async def defer(self, ephemeral=None):
        pass

    async def edit_message(self, content=None, embed=None, view=None):
        self.log.append(("edit_message", content))

    async def send_message(self, content=None, ephemeral=None):
        self.log.append(("send_message", content))

    async def send_modal(self, modal):
        self.log.append(("send_modal", modal))


class _Interaction:
    def __init__(self, user_id=7):
        self.followup, self.edits, self.log = _Followup(), [], []
        self.user = type("U", (), {"id": user_id})()
        self.response = _Response(self.log)

    async def edit_original_response(self, content=None, embed=None, view=None):
        self.edits.append((content, view))


PASSWORD = "correct horse battery staple"


def _hash(password, salt=b"0123456789abcdef", n=1024):
    import base64
    import hashlib
    h = hashlib.scrypt(password.encode(), salt=salt, n=n, r=8, p=1, maxmem=128 * 1024 * 1024, dklen=32)
    return f"scrypt${n}$8$1${base64.b64encode(salt).decode()}${base64.b64encode(h).decode()}"


def _plugin(releases, blobs, tmp_path, monkeypatch, dev_zip=None, with_password=True):
    monkeypatch.setattr(commands, "FH_REPORT_RELEASE", "14.4.3")
    monkeypatch.setattr(commands, "UPGRADE_DEV_PASSWORD_HASH", _hash(PASSWORD) if with_password else "")
    plugin = make_plugin(node=type("N", (), {"config_dir": str(tmp_path / "config")})())
    restarted = []

    async def restart():
        restarted.append(True)
    plugin.bot = type("B", (), {"node": type("N", (), {"restart": staticmethod(restart)})()})()

    async def http(url, as_json=False):
        if as_json:
            return releases
        if url.endswith("/zipball/dev"):
            if dev_zip is None:
                raise commands.UpgradeError("GitHub answered 404.")
            return dev_zip
        return blobs[url]
    plugin._http_get = http
    return plugin, restarted


def _run_command(plugin):
    interaction = _Interaction()
    asyncio.run(commands.Fh_Report.upgrade(plugin, interaction))
    return interaction


def test_up_to_date_says_so(tmp_path, monkeypatch):
    plugin, _ = _plugin([_rel("v.14.4.3")], {}, tmp_path, monkeypatch, dev_zip=_zip(version="14.4.3"))
    assert "newest version" in _run_command(plugin).followup.sent[0][0]


def test_a_newer_release_and_a_newer_dev_give_two_buttons_for_the_admin_only(tmp_path, monkeypatch):
    plugin, _ = _plugin([_rel("v.14.9.0")], {}, tmp_path, monkeypatch, dev_zip=_zip(version="14.9.5"))
    content, embed, view = _run_command(plugin).followup.sent[0]
    assert content is None and view.update_release.disabled is False and view.update_dev.disabled is False
    names = {f.name: f.value for f in embed.fields}
    assert "14.9.0" in names["Release"] and "14.9.5" in names["Development branch"] and "⚠️ DEVELOPMENT BRANCH" in names
    ok = type("I", (), {"user": type("U", (), {"id": 7})()})()
    other = type("I", (), {"user": type("U", (), {"id": 8})()})()
    assert asyncio.run(view.interaction_check(ok)) is True and asyncio.run(view.interaction_check(other)) is False


def test_only_what_is_newer_gets_an_enabled_button(tmp_path, monkeypatch):
    plugin, _ = _plugin([_rel("v.14.4.3")], {}, tmp_path, monkeypatch, dev_zip=_zip(version="14.9.5"))
    view = _run_command(plugin).followup.sent[0][2]
    assert view.update_release.disabled is True and view.update_dev.disabled is False
    plugin, _ = _plugin([_rel("v.14.9.0")], {}, tmp_path, monkeypatch, dev_zip=_zip(version="14.4.3"))
    view = _run_command(plugin).followup.sent[0][2]
    assert view.update_release.disabled is False and view.update_dev.disabled is True


def test_without_a_dev_password_the_dev_branch_is_not_even_checked(tmp_path, monkeypatch):
    plugin, _ = _plugin([_rel("v.14.9.0")], {}, tmp_path, monkeypatch, dev_zip=_zip(version="14.9.5"), with_password=False)
    _, embed, view = _run_command(plugin).followup.sent[0]
    assert view.update_dev.disabled is True and not any(f.name == "Development branch" for f in embed.fields)


def test_a_broken_dev_branch_is_reported_but_the_release_stays_available(tmp_path, monkeypatch):
    plugin, _ = _plugin([_rel("v.14.9.0")], {}, tmp_path, monkeypatch, dev_zip=None)
    _, embed, view = _run_command(plugin).followup.sent[0]
    assert view.update_release.disabled is False and view.update_dev.disabled is True
    assert any("development branch" in f.value for f in embed.fields)


# ── the dev password ──────────────────────────────────────────────────────────────

def test_the_stored_hash_accepts_only_the_right_password():
    stored = _hash(PASSWORD)
    assert commands._dev_password_ok(PASSWORD, stored) and PASSWORD not in stored
    assert not commands._dev_password_ok("wrong", stored) and not commands._dev_password_ok("", stored)
    assert not commands._dev_password_ok(PASSWORD, "") and not commands._dev_password_ok(PASSWORD, "garbage")


def test_three_wrong_passwords_lock_the_user_for_ten_minutes(tmp_path, monkeypatch):
    plugin, _ = _plugin([], {}, tmp_path, monkeypatch)
    check = lambda pw, now: plugin._dev_password_check(7, pw, now)           # noqa: E731
    assert check("a", 1000) == (False, "❌ Wrong password. 2 attempt(s) left.")
    assert check("b", 1001) == (False, "❌ Wrong password. 1 attempt(s) left.")
    assert "Locked for 10 minutes" in check("c", 1002)[1]
    ok, msg = check(PASSWORD, 1100)                                           # right password, but locked
    assert not ok and "Too many" in msg
    assert check(PASSWORD, 1002 + 601) == (True, "")                          # lock over
    assert check("a", 5000)[1].endswith("2 attempt(s) left.")                 # and the counter started again
    assert plugin._dev_password_check(8, "x", 1000)[1].endswith("2 attempt(s) left.")   # other users unaffected


def test_a_failed_password_never_reaches_the_log(tmp_path, monkeypatch, caplog):
    plugin, _ = _plugin([], {}, tmp_path, monkeypatch)
    with caplog.at_level("WARNING", logger="fh_report.tests"):
        plugin._dev_password_check(7, "super-secret-guess", 1000)
    assert caplog.records and "super-secret-guess" not in caplog.text


# ── installing and restarting ──────────────────────────────────────────────────────

def _confirm(plugin, release, user_id=7):
    interaction = _Interaction(user_id)
    asyncio.run(plugin._upgrade_run(interaction, release))
    return interaction


def test_a_release_update_installs_migrates_and_then_asks_about_the_restart(tmp_path, monkeypatch):
    d = _folder(tmp_path)
    monkeypatch.setattr(commands, "__file__", str(d / "commands.py"))
    rel = commands._pick_release([_rel("v.14.9.0")], (14, 4, 3))
    plugin, restarted = _plugin([], {rel["zip_url"]: _zip()}, tmp_path, monkeypatch)
    plugin._run_migration = lambda src: ""
    interaction = _confirm(plugin, rel)
    text, view = interaction.edits[-1]
    assert "14.9.0" in (d / "commands.py").read_text()
    assert "Updated to Ver. 14.9.0" in text and "restart" in text and restarted == []     # asked, not restarted
    assert isinstance(view, commands._RestartView)


def test_restart_now_restarts_and_later_does_not(tmp_path, monkeypatch):
    plugin, restarted = _plugin([], {}, tmp_path, monkeypatch)

    async def noop(_):
        pass
    monkeypatch.setattr(commands.asyncio, "sleep", noop)
    later = _Interaction()
    asyncio.run(commands._RestartView(plugin, 7, "14.9.0").later(later, None))
    assert restarted == [] and "next time DCSServerBot restarts" in later.log[0][1]
    now = _Interaction()
    asyncio.run(commands._RestartView(plugin, 7, "14.9.0").restart_now(now, None))
    assert restarted == [True] and "Restarting" in now.log[0][1]


def test_the_dev_update_installs_the_zip_that_was_offered(tmp_path, monkeypatch):
    d = _folder(tmp_path)
    monkeypatch.setattr(commands, "__file__", str(d / "commands.py"))
    plugin, restarted = _plugin([], {}, tmp_path, monkeypatch, dev_zip=_zip(version="14.9.5"))
    plugin._run_migration = lambda src: ""
    dev = asyncio.run(plugin._upgrade_lookup_dev())
    assert dev["text"] == "14.9.5" and dev["prerelease"] is True
    _confirm(plugin, dev)
    assert "14.9.5" in (d / "commands.py").read_text() and restarted == []


def test_the_password_dialog_gates_the_dev_update(tmp_path, monkeypatch):
    d = _folder(tmp_path)
    monkeypatch.setattr(commands, "__file__", str(d / "commands.py"))
    plugin, _ = _plugin([_rel("v.14.4.3")], {}, tmp_path, monkeypatch, dev_zip=_zip(version="14.9.5"))
    plugin._run_migration = lambda src: ""
    view = _run_command(plugin).followup.sent[0][2]
    press = _Interaction()
    asyncio.run(view.update_dev(press, None))
    modal = press.log[0][1]
    assert press.log[0][0] == "send_modal"
    modal.password = "wrong"
    bad = _Interaction()
    asyncio.run(modal.on_submit(bad))
    assert bad.log[0][0] == "send_message" and "Wrong password" in bad.log[0][1]
    assert (d / "commands.py").read_text() == "old commands.py"               # nothing installed
    modal.password = PASSWORD
    good = _Interaction()
    asyncio.run(modal.on_submit(good))
    assert "14.9.5" in (d / "commands.py").read_text()


def test_a_broken_release_installs_nothing_and_says_so(tmp_path, monkeypatch):
    d = _folder(tmp_path)
    monkeypatch.setattr(commands, "__file__", str(d / "commands.py"))
    rel = commands._pick_release([_rel("v.14.9.0")], (14, 4, 3))
    plugin, restarted = _plugin([], {rel["zip_url"]: _zip(version="14.8.0")}, tmp_path, monkeypatch)   # tag says 14.9.0
    interaction = _confirm(plugin, rel)
    assert (d / "commands.py").read_text() == "old commands.py" and restarted == []
    assert "does not match its tag" in interaction.edits[-1][0]


# ── the download itself (aiohttp replaced by a fake that hands the body over in pieces) ──

def _fake_aiohttp(monkeypatch, body: bytes, status=200, scheme="https", piece=1000):
    import sys
    import types

    class _Content:
        async def iter_chunked(self, n):
            for i in range(0, len(body), piece):
                yield body[i:i + piece]

    class _Resp:
        def __init__(self):
            self.status, self.content = status, _Content()
            self.url = type("U", (), {"scheme": scheme})()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

    class _Session:
        def __init__(self, **_):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        def get(self, url):
            return _Resp()

    mod = types.ModuleType("aiohttp")
    mod.ClientTimeout = lambda **_: None
    mod.ClientSession = _Session
    monkeypatch.setitem(sys.modules, "aiohttp", mod)


def test_a_body_that_arrives_in_pieces_is_read_whole(monkeypatch):
    import json
    body = json.dumps([{"tag_name": "v.14.9.0", "body": "x" * 20000}]).encode()      # far more than one piece
    _fake_aiohttp(monkeypatch, body)
    got = asyncio.run(make_plugin()._http_get("https://api.github.com/x", as_json=True))
    assert got[0]["tag_name"] == "v.14.9.0" and len(got[0]["body"]) == 20000
    assert asyncio.run(make_plugin()._http_get("https://api.github.com/x")) == body


def test_the_download_is_refused_when_too_big_not_https_or_not_200(monkeypatch):
    _fake_aiohttp(monkeypatch, b"x" * 3000)
    monkeypatch.setattr(commands, "UPGRADE_MAX_BYTES", 2000)
    with pytest.raises(commands.UpgradeError, match="larger"):
        asyncio.run(make_plugin()._http_get("https://h/x"))
    _fake_aiohttp(monkeypatch, b"x", status=404)
    with pytest.raises(commands.UpgradeError, match="404"):
        asyncio.run(make_plugin()._http_get("https://h/x"))
    _fake_aiohttp(monkeypatch, b"x", scheme="http")
    with pytest.raises(commands.UpgradeError, match="HTTPS"):
        asyncio.run(make_plugin()._http_get("https://h/x"))
    with pytest.raises(commands.UpgradeError, match="non-HTTPS"):
        asyncio.run(make_plugin()._http_get("http://h/x"))
