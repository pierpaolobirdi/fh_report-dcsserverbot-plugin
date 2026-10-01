"""_update_server end to end against a fake Discord channel."""
import asyncio
import os

from conftest import FileNode, commands, make_plugin

NotFound = commands.discord.NotFound


class _Author:
    id = 1


class _Message:
    def __init__(self, mid, title, log):
        self.id, self.author, self._log = mid, _Author(), log
        self.embeds = [type("E", (), {"title": title})()]

    async def edit(self, embed=None):
        self._log.append(("adopt_edit", self.id))


class _Partial:
    def __init__(self, channel, mid):
        self.channel, self.id = channel, mid

    async def edit(self, embed=None):
        self.channel.log.append(("partial_edit", self.id))
        if self.id not in self.channel.messages:
            raise NotFound()
        self.channel.sent = embed


class _Channel:
    def __init__(self, messages):
        self.messages, self.log = messages, []

    def get_partial_message(self, mid):
        return _Partial(self, mid)

    async def fetch_message(self, mid):
        self.log.append(("fetch", mid))

    async def send(self, embed=None):
        self.log.append(("send",))
        self.messages[99] = embed.title if embed else ""
        self.sent = embed
        return _Message(99, self.messages[99], self.log)

    def history(self, limit=50):
        async def gen():
            for mid, title in self.messages.items():
                yield _Message(mid, title, self.log)
        return gen()


class _Bot:
    user = _Author()

    def __init__(self, channel):
        self.channel = channel

    def get_channel(self, _):
        return self.channel


class _Server:
    status = None

    def __init__(self):
        self.node = FileNode()
        self.instance = type("I", (), {"name": "inst"})()


def _run(tmp_path, saves_dir, messages, ids, layout):
    d = saves_dir()
    channel = _Channel(messages)
    plugin = make_plugin(bot=_Bot(channel), _message_ids=dict(ids),
                         _message_ids_file=str(tmp_path / "message_ids.json"))
    server = _Server()
    asyncio.run(plugin._update_server(server, {"saves_dir": d, "channel_id": 5, "campaign_name": "C",
                                               "report_layout": layout}))
    return channel.log, plugin._message_ids, server.node.reads, d


def test_known_message_is_edited_with_one_call(tmp_path, saves_dir):
    log, ids, reads, _ = _run(tmp_path, saves_dir, {10: "📡  C"}, {"inst": 10}, "R")
    assert log == [("partial_edit", 10)] and ids == {"inst": 10}
    assert "daily_history.json" not in reads          # no Podium in this layout


def test_lost_message_id_adopts_existing_message(tmp_path, saves_dir):
    log, ids, reads, _ = _run(tmp_path, saves_dir, {10: "📡  C"}, {"inst": 7}, "RP")
    assert log == [("partial_edit", 7), ("adopt_edit", 10)] and ids == {"inst": 10}
    assert "daily_history.json" in reads


def test_empty_channel_gets_new_message(tmp_path, saves_dir):
    log, ids, _, d = _run(tmp_path, saves_dir, {}, {}, "D")
    assert log == [("send",)] and ids == {"inst": 99}
    assert os.path.isfile(os.path.join(d, ".fhc", "daily_snapshot.json"))


def test_no_local_folders_for_remote_paths(tmp_path):
    remote = str(tmp_path / "not" / "here" / "Saves")
    commands._ensure_local_fhc_dir(remote)
    assert not os.path.exists(os.path.dirname(remote))
