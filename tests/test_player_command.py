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

    async def fetchall(self):
        q = self.pool.queries[-1]
        if "GROUP BY kind" in q:
            return [("kill", 2, 1, 1), ("hit", 3, 2, 0)]
        if "ORDER BY i.time DESC" in q:
            return [(SEEN, "kill", "b" * 32, "Viper", "F-16C"), (SEEN, "hit", None, None, "T-72B")]
        if "FROM pu_events" in q:
            return [("kill", 1, 10.8), ("friendly_fire", 2, 4.8)]
        return []


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


def _invoke(saves_dir, player_name, admin, cfg_extra=None):
    d = saves_dir()
    pool, followup = _Pool(), _Followup()
    srv = type("S", (), {"node": FileNode(), "status": commands.Status.RUNNING, "name": "Public"})()
    plugin = make_plugin(apool=pool)
    plugin._is_admin = lambda interaction, server: admin

    async def ctx(interaction, server_param):
        return "inst", srv, {"saves_dir": d, **(cfg_extra or {})}, d, commands._UpdateReadCache(srv.node), True
    plugin._command_context = ctx
    plugin._public_server_name = lambda name: "Public"
    interaction = type("I", (), {"followup": followup, "user": type("U", (), {"id": 42})()})()
    asyncio.run(plugin.player(interaction, None, player_name))
    return pool, followup.sent


def test_self_lookup_uses_one_connection(saves_dir):
    pool, sent = _invoke(saves_dir, None, admin=False)
    # one connection for the account lookup, one for the optional BoB detail
    assert pool.connections == 2 and len(pool.queries) == 4   # lookup, mission start, BoB x2
    assert sum("discord_id" in q for q in pool.queries) == 1
    embed = sent[0]
    assert embed.title == "👤 Player — Zarpa"
    assert any("UCID" in f.value for f in embed.fields)


def test_admin_lookup_by_ucid(saves_dir):
    pool, sent = _invoke(saves_dir, A, admin=True)
    assert pool.connections == 2 and sent[0].title == "👤 Player — Zarpa"
    bnb = next(f for f in sent[0].fields if "Blue-on-Blue" in f.name).value
    assert "**Total:** 5 (2 destroyed · 3 damaged)" in bnb
    assert "**Today:** 3 · **Session:** 1" in bnb
    assert "Team kill → `Viper`" in bnb and bnb.endswith("Friendly fire → AI unit (T-72B)") and "pts" not in bnb
    assert not any("Penalties" in f.name for f in sent[0].fields)   # show_punishment is off


def test_non_admin_cannot_query_others(saves_dir):
    pool, sent = _invoke(saves_dir, "Viper", admin=False)
    assert pool.connections == 0 and "only view your own stats" in sent[0]


def test_missing_ranks_file_gives_friendly_message(saves_dir):
    import os
    d = saves_dir()
    os.remove(os.path.join(d, "Foothold_Ranks.lua"))
    pool, followup = _Pool(), _Followup()
    srv = type("S", (), {"node": FileNode(), "status": commands.Status.RUNNING, "name": "Public"})()
    plugin = make_plugin(apool=pool)
    plugin._is_admin = lambda interaction, server: False

    async def ctx(interaction, server_param):
        return "inst", srv, {"saves_dir": d}, d, commands._UpdateReadCache(srv.node), True
    plugin._command_context = ctx
    plugin._public_server_name = lambda name: "Public"
    interaction = type("I", (), {"followup": followup, "user": type("U", (), {"id": 42})()})()
    asyncio.run(plugin.player(interaction, None, None))
    assert followup.sent == ["❌ No campaign rankings yet for **Public** — fly a mission first, then try again."]


def test_fetch_bnb_counts_and_degrades_without_missionstats_plugin():
    class Cur(_Cursor):
        async def execute(self, query, args):
            await super().execute(query, args)
            if "missionstats" in query and self.pool.broken:
                raise RuntimeError('relation "missionstats" does not exist')

        async def fetchone(self):
            return (SEEN.replace(tzinfo=None),)

        async def fetchall(self):
            return [(A, 5, 3, 1)]

    class Conn(Cur):
        def cursor(self):
            return Cur(self.pool)

    class Pool(_Pool):
        broken = False

        def connection(self):
            self.connections += 1
            return Conn(self)

    srv = type("S", (), {"name": "Public"})()
    pool = Pool()
    plugin = make_plugin(apool=pool)
    assert asyncio.run(plugin._fetch_bnb(srv, {}, "/s", [A])) == {A: {"total": 5, "day": 3, "session": 1}}
    pool.broken = True
    assert asyncio.run(plugin._fetch_bnb(srv, {}, "/s", [A])) == {}


def test_penalties_in_force_follow_show_punishment(saves_dir):
    pool, sent = _invoke(saves_dir, A, admin=True, cfg_extra={"show_punishment": True})
    names = [f.name for f in sent[0].fields]
    assert names.index("⚠️ __Blue-on-Blue (BoB)__") < names.index("⚖️ __Penalties in force__")
    assert any("FROM pu_events" in q for q in pool.queries)
    pen = next(f for f in sent[0].fields if "Penalties" in f.name).value
    assert "JAG's investigation (15 p.p.) 🔨🔨" in pen          # 10.8 + 4.8 = 15.6 p.p.
    assert "Team kill ×1 (10.8 p.p.) · Friendly fire ×2 (4.8 p.p.)" in pen
    assert "decay" in pen
