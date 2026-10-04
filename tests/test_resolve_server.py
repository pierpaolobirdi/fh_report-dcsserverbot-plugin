"""Which server a /fh_report command applies to when the user does not pick one."""
from conftest import commands, make_plugin

Status = commands.Status


class _Srv:
    def __init__(self, instance, status=Status.RUNNING, managed_by=None):
        self.instance = type("I", (), {"name": instance})()
        self.status = status
        self.locals = {"managed_by": managed_by} if managed_by else {}
        self.name = instance


def _plugin(config, servers):
    plugin = make_plugin(bot=type("B", (), {"servers": servers})())
    plugin.locals = {"DEFAULT": {}, **{k: {"channel_id": 1} for k in config}}
    plugin._public_server_name = lambda n: n
    return plugin


def _interaction():
    return type("I", (), {"channel_id": 5, "user": object(), "command": None})()


def test_one_registered_server_among_leftover_blocks_needs_no_choice():
    """The sample DCS_Server block (or a server that is off) must not demand a choice."""
    plugin = _plugin(["Live", "DCS_Server"], {"Live": _Srv("Live")})
    assert plugin._resolve_server(_interaction(), None) == ("Live", None)


def test_an_unregistered_server_does_not_count_either():
    plugin = _plugin(["Live", "Offline"], {"Live": _Srv("Live"), "Offline": _Srv("Offline", Status.UNREGISTERED)})
    assert plugin._resolve_server(_interaction(), None) == ("Live", None)


def test_two_available_servers_still_require_the_server_option():
    plugin = _plugin(["A", "B"], {"A": _Srv("A"), "B": _Srv("B")})
    name, err = plugin._resolve_server(_interaction(), None)
    assert name is None and "More than one server" in err
    assert plugin._resolve_server(_interaction(), plugin.bot.servers["B"]) == ("B", None)


def test_the_option_still_rejects_a_server_without_a_block():
    plugin = _plugin(["A", "B"], {"A": _Srv("A"), "B": _Srv("B"), "C": _Srv("C")})
    name, err = plugin._resolve_server(_interaction(), plugin.bot.servers["C"])
    assert name is None and "isn't configured" in err


def test_nothing_registered_yet_falls_back_to_the_config_as_before():
    one = _plugin(["Only"], {})
    assert one._resolve_server(_interaction(), None) == ("Only", None)
    two = _plugin(["A", "B"], {})
    assert "More than one server" in two._resolve_server(_interaction(), None)[1]


def test_no_blocks_at_all():
    assert "no servers configured" in _plugin([], {})._resolve_server(_interaction(), None)[1]


def test_a_server_managed_by_another_role_is_not_counted_for_an_admin_command(monkeypatch):
    """An admin command only lists the servers whose managed_by roles the caller holds."""
    plugin = _plugin(["Mine", "Theirs"], {"Mine": _Srv("Mine"), "Theirs": _Srv("Theirs", managed_by=["Other Admins"])})
    monkeypatch.setattr(commands._FHServerTransformer, "is_admin", staticmethod(lambda i: True))
    assert plugin._resolve_server(_interaction(), None) == ("Mine", None)
    monkeypatch.setattr(commands._FHServerTransformer, "is_admin", staticmethod(lambda i: False))
    assert "More than one server" in plugin._resolve_server(_interaction(), None)[1]
