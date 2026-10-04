"""/fh_report update: admin only, refreshes the embed whatever the mission status."""
import asyncio

from conftest import commands, make_plugin


class _Followup:
    def __init__(self):
        self.sent = []

    async def send(self, content=None, embed=None, ephemeral=None):
        self.sent.append(content)


class _Response:
    async def defer(self, ephemeral=None):
        pass


class _Channel:
    def get_partial_message(self, mid):
        return type("M", (), {"jump_url": f"https://discord/m/{mid}"})()


def _run(admin=True, status=commands.Status.PAUSED, cfg=None, channel=True, fail=False):
    srv = type("S", (), {"status": status, "name": "Public"})()
    plugin = make_plugin(bot=type("B", (), {"get_channel": lambda self, _: _Channel() if channel else None})())
    plugin._message_ids = {"inst": 77}
    plugin._resolve_server = lambda interaction, param: ("inst", None)
    plugin._server_by_key = lambda key: srv
    plugin._is_admin = lambda interaction, key: admin
    plugin._merged_cfg = lambda key: {"channel_id": 5, **(cfg or {})}
    plugin._public_server_name = lambda key: "Public"
    calls = []

    async def fake_update(server, c):
        calls.append(server)
        if fail:
            raise RuntimeError("boom")
    plugin._update_server = fake_update
    followup = _Followup()
    interaction = type("I", (), {"followup": followup, "response": _Response()})()
    asyncio.run(plugin.update(interaction, None))
    return calls, followup.sent, srv


def test_an_admin_refreshes_a_paused_server_and_gets_the_link():
    calls, sent, srv = _run(status=commands.Status.PAUSED)
    assert calls == [srv] and sent == ["✅ Report of **Public** updated. https://discord/m/77"]


def test_stopped_servers_are_refreshed_too():
    calls, _, _ = _run(status=commands.Status.STOPPED)
    assert len(calls) == 1


def test_a_non_admin_is_refused_and_nothing_runs():
    calls, sent, _ = _run(admin=False)
    assert calls == [] and "Only admins" in sent[0]


def test_a_switched_off_server_says_so():
    calls, sent, _ = _run(cfg={"enable_updates": False})
    assert calls == [] and "enable_updates: false" in sent[0]


def test_no_report_channel_is_explained():
    calls, sent, _ = _run(channel=False)
    assert calls == [] and "no usable report channel" in sent[0]


def test_a_failed_update_is_reported_not_raised():
    calls, sent, _ = _run(fail=True)
    assert len(calls) == 1 and "The update failed" in sent[0] and "boom" in sent[0]


def test_the_same_server_is_never_updated_twice_at_once():
    plugin = make_plugin()
    order = []

    async def slow(server, cfg):
        order.append("start")
        await asyncio.sleep(0.01)
        order.append("end")
    plugin._update_server_locked = slow
    srv = type("S", (), {"instance": type("I", (), {"name": "i"})(), "node": None})()

    async def both():
        await asyncio.gather(plugin._update_server(srv, {}), plugin._update_server(srv, {}))
    asyncio.run(both())
    assert order == ["start", "end", "start", "end"]
