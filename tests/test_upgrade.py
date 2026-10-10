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
        msg = type("Msg", (), {})()
        msg.deleted = []

        async def delete():
            msg.deleted.append(True)
        msg.delete = delete
        self.last = msg
        return msg


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
        self.followup, self.edits, self.log, self.deleted = _Followup(), [], [], []
        self.user = type("U", (), {"id": user_id})()
        self.response = _Response(self.log)
        self.application_id, self.token, self.message = 555, "tok-secret", type("M", (), {"id": 999})()

    async def edit_original_response(self, content=None, embed=None, view=None):
        self.edits.append((content, view))

    async def delete_original_response(self):
        self.deleted.append(True)


def _plugin(releases, blobs, tmp_path, monkeypatch, dev_zip=None):
    monkeypatch.setattr(commands, "FH_REPORT_RELEASE", "14.4.3")
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


def test_dev_is_offered_only_when_it_is_above_the_release_too(tmp_path, monkeypatch):
    def buttons(release_tag, dev_version):
        plugin, _ = _plugin([_rel(release_tag)], {}, tmp_path, monkeypatch, dev_zip=_zip(version=dev_version))
        view = _run_command(plugin).followup.sent[0][2]
        return (not view.update_release.disabled, not view.update_dev.disabled)
    assert buttons("v.14.9.0", "14.9.0") == (True, False)        # same version: only the release
    assert buttons("v.14.9.0", "14.8.0") == (True, False)        # dev behind the release: only the release
    assert buttons("v.14.9.0", "14.9.1") == (True, True)         # dev ahead of the release: both


def test_a_broken_dev_branch_is_reported_but_the_release_stays_available(tmp_path, monkeypatch):
    plugin, _ = _plugin([_rel("v.14.9.0")], {}, tmp_path, monkeypatch, dev_zip=None)
    _, embed, view = _run_command(plugin).followup.sent[0]
    assert view.update_release.disabled is False and view.update_dev.disabled is True
    assert any("development branch" in f.value for f in embed.fields)


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


# ── messages that clean themselves up, and the notice that closes a restart ───────

def _timed(monkeypatch, plugin, scenario):
    """Run `scenario()` (async) and return the sleeps requested by scheduled clean-ups, having let them finish."""
    waits = []

    async def fake_sleep(seconds):
        waits.append(seconds)
    monkeypatch.setattr(commands.asyncio, "sleep", fake_sleep)

    async def go():
        await scenario()
        await asyncio.gather(*list(plugin.__dict__.get("_temp_replies", ())))
    asyncio.run(go())
    return waits


def test_the_newest_version_message_goes_after_10_seconds_and_errors_after_30(tmp_path, monkeypatch):
    plugin, _ = _plugin([_rel("v.14.4.3")], {}, tmp_path, monkeypatch, dev_zip=_zip(version="14.4.3"))
    interaction = _Interaction()
    waits = _timed(monkeypatch, plugin, lambda: commands.Fh_Report.upgrade(plugin, interaction))
    assert waits == [10] and interaction.followup.last.deleted == [True]

    async def broken(url, as_json=False):
        raise commands.UpgradeError("GitHub answered 500.")
    plugin._http_get = broken
    interaction = _Interaction()
    waits = _timed(monkeypatch, plugin, lambda: commands.Fh_Report.upgrade(plugin, interaction))
    assert waits == [30] and "500" in interaction.followup.sent[0][0]


def test_cancel_goes_after_5_seconds_and_an_expired_offer_after_10(tmp_path, monkeypatch):
    plugin, _ = _plugin([_rel("v.14.9.0")], {}, tmp_path, monkeypatch, dev_zip=_zip(version="14.4.3"))
    view = _run_command(plugin).followup.sent[0][2]
    cancel = _Interaction()
    assert _timed(monkeypatch, plugin, lambda: view.cancel(cancel, None)) == [5] and cancel.deleted == [True]
    view = _run_command(plugin).followup.sent[0][2]
    view.interaction = expired = _Interaction()
    assert _timed(monkeypatch, plugin, view.on_timeout) == [10] and expired.deleted == [True]


def test_a_failed_update_message_goes_after_30_seconds(tmp_path, monkeypatch):
    d = _folder(tmp_path)
    monkeypatch.setattr(commands, "__file__", str(d / "commands.py"))
    rel = commands._pick_release([_rel("v.14.9.0")], (14, 4, 3))
    plugin, _ = _plugin([], {rel["zip_url"]: _zip(version="14.8.0")}, tmp_path, monkeypatch)
    interaction = _Interaction()
    waits = _timed(monkeypatch, plugin, lambda: plugin._upgrade_run(interaction, rel))
    assert waits == [30] and interaction.deleted == [True]


class _Hook:
    def __init__(self, refuse=()):
        self.edits, self.deleted, self.refuse = [], [], refuse

    async def edit_message(self, message_id, **kw):
        if message_id in self.refuse:
            raise RuntimeError(f"404 for {message_id}")
        self.edits.append((message_id, kw["content"]))

    async def delete_message(self, message_id):
        self.deleted.append(message_id)


def test_restart_now_leaves_a_notice_for_the_new_process(tmp_path, monkeypatch):
    import json
    d = _folder(tmp_path)
    monkeypatch.setattr(commands, "__file__", str(d / "commands.py"))
    plugin, restarted = _plugin([], {}, tmp_path, monkeypatch)
    _timed(monkeypatch, plugin, lambda: commands._RestartView(plugin, 7, "14.9.0").restart_now(_Interaction(), None))
    data = json.loads((d / ".restart_notice.json").read_text())
    assert restarted == [True] and data["expected"] == "14.9.0" and data["message_id"] == 999 and data["token"] == "tok-secret"


def _notice(tmp_path, monkeypatch, expected, age=0, raw=None):
    import json
    import time
    d = _folder(tmp_path)
    monkeypatch.setattr(commands, "__file__", str(d / "commands.py"))
    plugin, _ = _plugin([], {}, tmp_path, monkeypatch)
    hook = _Hook()
    plugin._notice_webhook = lambda app_id, token: hook
    path = d / ".restart_notice.json"
    path.write_text(raw if raw is not None else json.dumps(
        {"app_id": 555, "token": "t", "message_id": 999, "expected": expected, "created": time.time() - age}))
    waits = _timed(monkeypatch, plugin, plugin._finish_restart_notice)
    return hook, waits, path


def test_the_restart_notice_reports_ok_or_a_mismatch(tmp_path, monkeypatch):
    import json
    import time
    for expected, current, mark in (("14.9.0", "14.9.0", "✅"), ("14.9.0", "14.4.3", "⚠️")):
        d = tmp_path / expected.replace(".", "") / mark.strip("️")
        d.mkdir(parents=True)
        folder = _folder(d)
        monkeypatch.setattr(commands, "__file__", str(folder / "commands.py"))
        plugin, _ = _plugin([], {}, tmp_path, monkeypatch)
        monkeypatch.setattr(commands, "FH_REPORT_RELEASE", current)
        hook = _Hook()
        plugin._notice_webhook = lambda app_id, token, hook=hook: hook
        (folder / ".restart_notice.json").write_text(json.dumps(
            {"app_id": 1, "token": "t", "message_id": 42, "expected": expected, "created": time.time()}))
        waits = _timed(monkeypatch, plugin, plugin._finish_restart_notice)
        assert hook.edits[0][0] == "@original" and hook.edits[0][1].startswith(mark)    # the button's own message first
        assert waits == [20] and hook.deleted == ["@original"]
        assert not (folder / ".restart_notice.json").exists()


def test_an_old_or_unreadable_notice_is_deleted_without_touching_discord(tmp_path, monkeypatch):
    (tmp_path / "old").mkdir()
    (tmp_path / "broken").mkdir()
    hook, waits, path = _notice(tmp_path / "old", monkeypatch, expected="14.4.3", age=15 * 60)
    assert hook.edits == [] and waits == [] and not path.exists()
    hook, waits, path = _notice(tmp_path / "broken", monkeypatch, expected="x", raw="{not json")
    assert hook.edits == [] and waits == [] and not path.exists()


def test_no_notice_file_means_nothing_happens(tmp_path, monkeypatch):
    d = _folder(tmp_path)
    monkeypatch.setattr(commands, "__file__", str(d / "commands.py"))
    plugin, _ = _plugin([], {}, tmp_path, monkeypatch)
    plugin._notice_webhook = lambda *a: (_ for _ in ()).throw(AssertionError("must not be used"))
    asyncio.run(plugin._finish_restart_notice())


# ── the development branch asks to accept the risk, not for a password ─────────────

def test_the_dev_button_shows_a_risk_warning_and_installs_only_after_accepting(tmp_path, monkeypatch):
    d = _folder(tmp_path)
    monkeypatch.setattr(commands, "__file__", str(d / "commands.py"))
    plugin, restarted = _plugin([_rel("v.14.4.3")], {}, tmp_path, monkeypatch, dev_zip=_zip(version="14.9.5"))
    plugin._run_migration = lambda src: ""
    view = _run_command(plugin).followup.sent[0][2]
    press = _Interaction()
    asyncio.run(view.update_dev(press, None))
    kind, content = press.log[0]
    assert kind == "edit_message" and (d / "commands.py").read_text() == "old commands.py"      # warned, nothing installed
    warning = commands._dev_warning_embed({"text": "14.9.5"})
    assert "errors" in " ".join(f.value for f in warning.fields) and "14.9.5" in warning.description
    warn_view = commands._DevWarningView(plugin, 7, {"text": "14.9.5", "data": _zip(version="14.9.5")})
    assert asyncio.run(warn_view.interaction_check(type("I", (), {"user": type("U", (), {"id": 8})()})())) is False
    asyncio.run(warn_view.accept(_Interaction(), None))
    assert "14.9.5" in (d / "commands.py").read_text() and restarted == []


def test_cancel_and_expiry_of_the_risk_warning_clean_up(tmp_path, monkeypatch):
    plugin, _ = _plugin([], {}, tmp_path, monkeypatch)
    cancel = _Interaction()
    view = commands._DevWarningView(plugin, 7, {"text": "14.9.5"})
    assert _timed(monkeypatch, plugin, lambda: view.cancel(cancel, None)) == [5] and cancel.deleted == [True]
    view.interaction = expired = _Interaction()
    assert _timed(monkeypatch, plugin, view.on_timeout) == [10] and expired.deleted == [True]


def test_later_and_an_unanswered_restart_question_clean_up_after_30_seconds(tmp_path, monkeypatch):
    plugin, restarted = _plugin([], {}, tmp_path, monkeypatch)
    later = _Interaction()
    view = commands._RestartView(plugin, 7, "14.9.0")
    assert _timed(monkeypatch, plugin, lambda: view.later(later, None)) == [30] and later.deleted == [True]
    view = commands._RestartView(plugin, 7, "14.9.0")
    view.interaction = unanswered = _Interaction()
    assert _timed(monkeypatch, plugin, view.on_timeout) == [30] and unanswered.deleted == [True]
    assert restarted == []                                                      # neither of them restarts anything


def _notice_with(tmp_path, monkeypatch, hook):
    import json
    import time
    d = _folder(tmp_path)
    monkeypatch.setattr(commands, "__file__", str(d / "commands.py"))
    monkeypatch.setattr(commands, "FH_REPORT_RELEASE", "14.9.0")
    plugin, _ = _plugin([], {}, tmp_path, monkeypatch)
    plugin._notice_webhook = lambda app_id, token: hook
    (d / ".restart_notice.json").write_text(json.dumps(
        {"app_id": 1, "token": "t", "message_id": 42, "expected": "14.9.0", "created": time.time()}))
    return plugin, _timed(monkeypatch, plugin, plugin._finish_restart_notice)


def test_if_discord_refuses_the_original_the_message_id_is_tried(tmp_path, monkeypatch):
    hook = _Hook(refuse=("@original",))
    plugin, waits = _notice_with(tmp_path, monkeypatch, hook)
    assert hook.edits[0][0] == 42 and waits == [20] and hook.deleted == [42]


def test_if_both_ways_fail_it_is_logged_as_a_warning(tmp_path, monkeypatch, caplog):
    hook = _Hook(refuse=("@original", 42))
    with caplog.at_level("WARNING", logger="fh_report.tests"):
        plugin, waits = _notice_with(tmp_path, monkeypatch, hook)
    assert hook.edits == [] and waits == [] and "could not update the restart message" in caplog.text
