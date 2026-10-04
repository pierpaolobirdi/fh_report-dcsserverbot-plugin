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


def test_blocks_for_servers_that_are_not_there_yet_say_the_server_is_still_registering():
    for names in (["Only"], ["A", "B"]):
        err = _plugin(names, {})._resolve_server(_interaction(), None)[1]
        assert "isn't currently available" in err


def test_no_blocks_at_all():
    assert "no servers configured" in _plugin([], {})._resolve_server(_interaction(), None)[1]


def test_a_server_managed_by_another_role_is_not_counted_for_an_admin_command(monkeypatch):
    """An admin command only lists the servers whose managed_by roles the caller holds."""
    plugin = _plugin(["Mine", "Theirs"], {"Mine": _Srv("Mine"), "Theirs": _Srv("Theirs", managed_by=["Other Admins"])})
    monkeypatch.setattr(commands._FHServerTransformer, "is_admin", staticmethod(lambda i: True))
    assert plugin._resolve_server(_interaction(), None) == ("Mine", None)
    monkeypatch.setattr(commands._FHServerTransformer, "is_admin", staticmethod(lambda i: False))
    assert "More than one server" in plugin._resolve_server(_interaction(), None)[1]


def _with_channel_server(plugin, server):
    plugin.bot.get_server = lambda interaction: server
    return plugin


def test_with_several_servers_an_empty_option_means_the_server_of_the_channel():
    """DCSServerBot's list shows only the channel's server; pressing Enter must use it."""
    plugin = _plugin(["A", "B"], {"A": _Srv("A"), "B": _Srv("B")})
    _with_channel_server(plugin, plugin.bot.servers["B"])
    assert plugin._resolve_server(_interaction(), None) == ("B", None)


def test_an_explicit_choice_beats_the_channel():
    plugin = _plugin(["A", "B"], {"A": _Srv("A"), "B": _Srv("B")})
    _with_channel_server(plugin, plugin.bot.servers["B"])
    assert plugin._resolve_server(_interaction(), plugin.bot.servers["A"]) == ("A", None)


def test_a_channel_without_one_of_the_available_servers_still_asks_and_names_them():
    plugin = _plugin(["A", "B"], {"A": _Srv("A"), "B": _Srv("B"), "C": _Srv("C")})
    for channel_server in (None, plugin.bot.servers["C"]):          # no server there / a server without a block
        _with_channel_server(plugin, channel_server)
        name, err = plugin._resolve_server(_interaction(), None)
        assert name is None and "More than one server is available (**A**, **B**)" in err


def test_the_channel_lookup_failing_does_not_break_the_command():
    plugin = _plugin(["A", "B"], {"A": _Srv("A"), "B": _Srv("B")})
    plugin.bot.get_server = lambda interaction: (_ for _ in ()).throw(RuntimeError("boom"))
    assert "More than one server" in plugin._resolve_server(_interaction(), None)[1]


def test_the_channel_server_still_goes_through_the_commands_channel_check():
    plugin = _plugin(["A", "B"], {"A": _Srv("A"), "B": _Srv("B")})
    plugin.locals["B"]["commands_channel_id"] = "999"
    _with_channel_server(plugin, plugin.bot.servers["B"])
    name, err = plugin._resolve_server(_interaction(), None)
    assert name is None and "can't be used in this channel" in err
