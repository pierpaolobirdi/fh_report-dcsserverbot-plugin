"""/fh_report player: self lookup, admin lookup by UCID, one DB connection per call."""
import asyncio
from datetime import datetime, timezone

from conftest import FileNode, commands, make_plugin

A = "a" * 32
SEEN = datetime(2026, 9, 1, tzinfo=timezone.utc)


class _Cursor:
    def __init__(self, pool):
        self.pool = pool

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass

    async def execute(self, query, args):
        self.pool.queries.append(" ".join(query.split()))

    async def fetchone(self):
        q = self.pool.queries[-1]
        return (A, SEEN) if "discord_id" in q else (SEEN,)


class _Connection(_Cursor):
    def cursor(self):
        return _Cursor(self.pool)


class _Pool:
    def __init__(self):
        self.connections, self.queries = 0, []

    def connection(self):
        self.connections += 1
        return _Connection(self)


class _Followup:
    def __init__(self):
        self.sent = []

    async def send(self, content=None, embed=None, ephemeral=None):
        self.sent.append(embed or content)


def _invoke(saves_dir, player_name, admin):
    d = saves_dir()
    pool, followup = _Pool(), _Followup()
    srv = type("S", (), {"node": FileNode(), "status": commands.Status.RUNNING, "name": "Public"})()
    plugin = make_plugin(apool=pool)
    plugin._is_admin = lambda interaction, server: admin

    async def ctx(interaction, server_param):
        return "inst", srv, {"saves_dir": d}, d, commands._UpdateReadCache(srv.node), True
    plugin._command_context = ctx
    plugin._public_server_name = lambda name: "Public"
    interaction = type("I", (), {"followup": followup, "user": type("U", (), {"id": 42})()})()
    asyncio.run(plugin.player(interaction, None, player_name))
    return pool, followup.sent


def test_self_lookup_uses_one_connection(saves_dir):
    pool, sent = _invoke(saves_dir, None, admin=False)
    assert pool.connections == 1 and len(pool.queries) == 1
    embed = sent[0]
    assert embed.title == "👤 Player — Zarpa"
    assert any("UCID" in f.value for f in embed.fields)


def test_admin_lookup_by_ucid(saves_dir):
    pool, sent = _invoke(saves_dir, A, admin=True)
    assert pool.connections == 1 and sent[0].title == "👤 Player — Zarpa"


def test_non_admin_cannot_query_others(saves_dir):
    pool, sent = _invoke(saves_dir, "Viper", admin=False)
    assert pool.connections == 0 and "only view your own stats" in sent[0]
