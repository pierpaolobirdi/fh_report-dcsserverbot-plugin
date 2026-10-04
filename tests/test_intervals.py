"""update_interval per server: the shortest interval in the file sets the beat; the rest wait whole beats."""
from conftest import commands
from test_multinode import A, B, _Srv, _plugin


def _run(plugin, servers, beats):
    """Servers updated on each of `beats` consecutive beats."""
    return [[s.name for s in plugin._due_servers(servers)] for _ in range(beats)]


def _setup(config, servers, base=300):
    plugin = _plugin(config, servers)
    plugin._base_interval = base
    plugin._beat = plugin._compute_beat(base)
    return plugin


def test_without_own_intervals_nothing_changes():
    plugin = _setup({"DCS.dcs_serverrelease": {"channel_id": 1}}, [A])
    assert plugin._beat == 300
    assert _run(plugin, [A], 3) == [[A.name]] * 3


def test_the_shortest_interval_sets_the_beat_and_the_others_wait_whole_beats():
    cfg = {"MyNode1": {"DCS.dcs_serverrelease": {"channel_id": 1, "update_interval": 60}},
           "MyNode2": {"DCS.dcs_serverrelease": {"channel_id": 2, "update_interval": 600}},
           "DCS.other": {"channel_id": 3}}                                   # 300 from DEFAULT
    other = _Srv("MyNode1", "DCS.other")
    plugin = _setup(cfg, [A, B, other])
    assert plugin._beat == 60
    rounds = _run(plugin, [A, B, other], 11)
    assert [A.name in r for r in rounds] == [True] * 11                      # every beat
    assert [other.name in r for r in rounds] == [i % 5 == 0 for i in range(11)]    # every 5 beats
    assert [B.name in r for r in rounds] == [i % 10 == 0 for i in range(11)]       # every 10 beats


def test_an_interval_between_multiples_is_rounded_up():
    cfg = {"DCS.a": {"channel_id": 1, "update_interval": 60}, "DCS.b": {"channel_id": 2, "update_interval": 100}}
    a, b = _Srv("N", "DCS.a"), _Srv("N", "DCS.b")
    plugin = _setup(cfg, [a, b])
    assert [b.name in r for r in _run(plugin, [a, b], 5)] == [True, False, True, False, True]    # every 120 s


def test_an_own_interval_ignores_the_default_even_when_it_is_longer():
    plugin = _setup({"DCS.a": {"channel_id": 1, "update_interval": 900}}, [_Srv("N", "DCS.a")], base=300)
    assert plugin._beat == 300                                                # DEFAULT still drives the loop
    a = plugin._configured_servers()[0]
    assert plugin._interval_for(a) == 900
    assert [a.name in r for r in _run(plugin, [a], 7)] == [True, False, False, True, False, False, True]


def test_an_invalid_own_interval_falls_back_to_default_and_warns_once(caplog):
    plugin = _setup({"DCS.a": {"channel_id": 1, "update_interval": "soon"}}, [_Srv("N", "DCS.a")])
    a = plugin._configured_servers()[0]
    with caplog.at_level("WARNING", logger="fh_report.tests"):
        assert plugin._interval_for(a) == 300 and plugin._interval_for(a) == 300
    assert sum("update_interval" in r.message for r in caplog.records) == 1
    assert plugin._beat == 300


def test_a_server_that_shows_up_late_is_updated_at_once():
    plugin = _setup({"DCS.a": {"channel_id": 1, "update_interval": 60}, "DCS.b": {"channel_id": 2}},
                    [_Srv("N", "DCS.a"), _Srv("N", "DCS.b")])
    a, b = _Srv("N", "DCS.a"), _Srv("N", "DCS.b")
    plugin._due_servers([a]); plugin._due_servers([a])
    assert b in plugin._due_servers([a, b])
