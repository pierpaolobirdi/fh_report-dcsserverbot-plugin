"""Servers are identified by node + instance: instance names repeat across the nodes of a cluster."""
import asyncio
import logging

import pytest

from conftest import FileNode, commands, make_plugin
from test_update_server import _Bot, _Channel

Status = commands.Status


class _Node(FileNode):
    def __init__(self, name):
        super().__init__()
        self.name = name


class _Srv:
    def __init__(self, node, instance="DCS.dcs_serverrelease", public=None, status=Status.RUNNING):
        self.node = _Node(node) if node else FileNode()
        self.instance = type("I", (), {"name": instance})()
        self.name = public or f"{node}-{instance}"
        self.status = status
        self.locals = {}


def _plugin(config, servers):
    plugin = make_plugin(bot=type("B", (), {"servers": {s.name: s for s in servers}})())
    plugin.locals = {"DEFAULT": {"report_layout": "R"}, **config}
    plugin._message_ids = {}
    return plugin


A, B = _Srv("MyNode1"), _Srv("MyNode2")
NODE_FORM = {"MyNode1": {"DCS.dcs_serverrelease": {"channel_id": 111, "campaign_name": "Server A"}},
             "MyNode2": {"DCS.dcs_serverrelease": {"channel_id": 222, "campaign_name": "Server B"}}}


def test_the_same_instance_name_on_two_nodes_gets_its_own_block_each():
    plugin = _plugin(NODE_FORM, [A, B])
    assert plugin._configured_servers() == [A, B]
    assert plugin._merged_cfg(A)["channel_id"] == 111 and plugin._merged_cfg(B)["channel_id"] == 222
    assert plugin._merged_cfg(A)["report_layout"] == "R"                       # DEFAULT still applies to both
    assert commands._server_key(A) == "MyNode1/DCS.dcs_serverrelease" != commands._server_key(B)
    assert plugin._merged_cfg("MyNode2/DCS.dcs_serverrelease")["campaign_name"] == "Server B"   # by key too


def test_the_flat_layout_of_every_earlier_version_keeps_working():
    plugin = _plugin({"DCS.dcs_serverrelease": {"channel_id": 5, "campaign_name": "C"}}, [A])
    assert plugin._configured_servers() == [A] and plugin._merged_cfg(A)["channel_id"] == 5


def test_both_layouts_can_share_one_file_and_the_node_entry_wins():
    other = _Srv("MyNode1", "DCS.other")
    plugin = _plugin({**NODE_FORM, "DCS.other": {"channel_id": 9, "campaign_name": "O"},
                      "DCS.dcs_serverrelease": {"channel_id": 1, "campaign_name": "flat"}}, [A, B, other])
    assert plugin._merged_cfg(A)["channel_id"] == 111                           # node entry beats the flat one
    assert plugin._merged_cfg(other)["channel_id"] == 9                         # flat entry for a node without a block


def test_a_flat_block_cannot_tell_two_servers_apart_so_neither_gets_a_report():
    plugin = _plugin({"DCS.dcs_serverrelease": {"channel_id": 5, "campaign_name": "C"}}, [A, B])
    assert plugin._configured_servers() == [] and not plugin._has_block(A) and not plugin._has_block(B)
    assert plugin._ambiguous_flat() == {"DCS.dcs_serverrelease"}


def test_an_unregistered_twin_does_not_make_a_flat_block_ambiguous():
    twin = _Srv("MyNode2", status=Status.UNREGISTERED)
    plugin = _plugin({"DCS.dcs_serverrelease": {"channel_id": 5, "campaign_name": "C"}}, [A, twin])
    assert plugin._configured_servers()[0] is A and A in plugin._configured_servers()


def test_the_ambiguity_is_logged_once_and_says_how_to_fix_it(caplog):
    commands._ambiguous_warned.clear()
    plugin = _plugin({"DCS.dcs_serverrelease": {"channel_id": 5, "campaign_name": "C"}}, [A, B])
    plugin.log = logging.getLogger("fh_report.tests")
    with caplog.at_level(logging.WARNING, logger="fh_report.tests"):
        plugin._warn_config_mismatches(plugin.locals)
        plugin._warn_config_mismatches(plugin.locals)
    msgs = [r.getMessage() for r in caplog.records if "several nodes" in r.getMessage()]
    assert len(msgs) == 1 and "MyNode1, MyNode2" in msgs[0] and "<node>: DCS.dcs_serverrelease" in msgs[0]


def test_blocks_that_match_nothing_are_reported_after_the_grace_period(caplog, monkeypatch):
    commands._unmatched_instance_since.clear()
    commands._unmatched_instance_warned.clear()
    monkeypatch.setattr(commands, "_UNMATCHED_INSTANCE_GRACE_SECONDS", 0)
    plugin = _plugin({"MyNode1": {"DCS.dcs_serverrelease": {"channel_id": 1}, "DCS.gone": {"channel_id": 2}},
                      "MyNod2": {"DCS.dcs_serverrelease": {"channel_id": 3}},        # a typo'd node name
                      "DCS.nowhere": {"channel_id": 4}}, [A, B])
    plugin.log = logging.getLogger("fh_report.tests")
    with caplog.at_level(logging.WARNING, logger="fh_report.tests"):
        plugin._warn_config_mismatches(plugin.locals)
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "'MyNode1/DCS.gone'" in text and "node 'MyNode1' has no instance named 'DCS.gone'" in text
    assert "'MyNod2'" in text and "looks like a node block, but no node has this name" in text
    assert "'DCS.nowhere'" in text and "'MyNode1/DCS.dcs_serverrelease'" not in text


def test_message_ids_of_earlier_versions_move_to_their_server_or_are_dropped(monkeypatch):
    saved = []
    plugin = _plugin(NODE_FORM, [A, B, _Srv("MyNode1", "DCS.solo")])
    plugin._save_message_ids = lambda: saved.append(dict(plugin._message_ids))
    plugin._message_ids = {"DCS.solo": 7, "DCS.dcs_serverrelease": 8, "DCS.unknown": 9, "MyNode1/DCS.dcs_serverrelease": 10}
    plugin._migrate_message_ids()
    assert plugin._message_ids == {"MyNode1/DCS.solo": 7, "DCS.unknown": 9, "MyNode1/DCS.dcs_serverrelease": 10}
    assert saved                                            # the ambiguous 8 is gone: found again by searching the channel
    saved.clear()
    plugin._migrate_message_ids()
    assert not saved                                        # repeating it changes nothing


def test_commands_pick_the_right_server_when_two_nodes_use_the_same_instance_name():
    plugin = _plugin(NODE_FORM, [A, B])
    interaction = type("I", (), {"channel_id": 5, "user": object(), "command": None})()
    plugin.bot.get_server = lambda i: None
    name, err = plugin._resolve_server(interaction, None)
    assert name is None and "**MyNode1-DCS.dcs_serverrelease**, **MyNode2-DCS.dcs_serverrelease**" in err
    assert plugin._resolve_server(interaction, B) == ("MyNode2/DCS.dcs_serverrelease", None)
    plugin.bot.get_server = lambda i: A                                  # the channel DCSSB assigns to A
    assert plugin._resolve_server(interaction, None) == ("MyNode1/DCS.dcs_serverrelease", None)
    assert plugin._server_by_key("MyNode2/DCS.dcs_serverrelease") is B
    assert plugin._server_by_key("DCS.dcs_serverrelease") is None        # a bare name is ambiguous here


def test_two_servers_with_the_same_instance_name_each_keep_their_own_message(tmp_path, saved_ranks):
    """End to end: each node's server posts to its own channel and remembers its own message."""
    chan_a, chan_b = _Channel({}), _Channel({})
    channels = {111: chan_a, 222: chan_b}
    plugin = _plugin(NODE_FORM, [A, B])
    plugin.bot = type("B", (), {"servers": plugin.bot.servers, "user": None,
                                "get_channel": lambda self, cid: channels[int(cid)]})()
    plugin._message_ids_file = str(tmp_path / "ids.json")
    for server in (A, B):
        cfg = plugin._merged_cfg(server)
        server.node = _Node(server.node.name)
        cfg["saves_dir"] = saved_ranks
        asyncio.run(plugin._update_server(server, cfg))
    assert set(plugin._message_ids) == {"MyNode1/DCS.dcs_serverrelease", "MyNode2/DCS.dcs_serverrelease"}
    assert chan_a.sent.title.endswith("Server A") and chan_b.sent.title.endswith("Server B")


@pytest.fixture
def saved_ranks(tmp_path_factory):
    import os
    import shutil
    from conftest import FIXTURES
    d = tmp_path_factory.mktemp("saves")
    shutil.copy(os.path.join(FIXTURES, "ranks_new.lua"), d / "Foothold_Ranks.lua")
    shutil.copy(os.path.join(FIXTURES, "foothold_new.lua"), d / "foothold_x.lua")
    return str(d)
