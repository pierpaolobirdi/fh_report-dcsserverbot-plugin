"""Daily tracking over simulated weeks: day rollovers, mid-day mission swaps,
in-place campaign resets. Golden file pins the exact daily figures plus the
resulting fhr_daily_snapshot.json and fhr_daily_history.json."""
import asyncio
import os
import random
from datetime import datetime as real_datetime, timedelta, timezone

import pytest

from conftest import UCID, FileNode, check_golden, commands, digest, make_plugin


class _Clock:
    now = None


class _FakeDatetime(real_datetime):
    @classmethod
    def now(cls, tz=None):
        return _Clock.now


@pytest.fixture
def fake_clock(monkeypatch):
    monkeypatch.setattr(commands, "datetime", _FakeDatetime)
    return _Clock


async def _scenario(saves, seed, clock):
    rnd = random.Random(seed)
    plugin = make_plugin()
    clock.now = real_datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)
    ids = UCID[:6]
    pts = {u: 0 for u in ids}
    stats = {u: {} for u in ids}
    filename, out = "foothold_a.lua", []
    for step in range(120):
        clock.now += timedelta(hours=rnd.choice([1, 2, 3, 5]))
        event = rnd.random()
        if event < 0.08:            # mission/map swap: new, empty save file
            filename = f"foothold_{step}.lua"
            pts, stats = {u: 0 for u in ids}, {u: {} for u in ids}
        elif event < 0.12:          # in-place reset: points and kills drop
            pts, stats = {u: v // 4 for u, v in pts.items()}, {u: {} for u in ids}
        for u in rnd.sample(ids, 3):
            pts[u] += rnd.randint(0, 200)
            key = rnd.choice(["Air", "Ground Units", "SAM", "CAP mission"])
            stats[u][key] = stats[u].get(key, 0) + 1
        cs = {u: v for u, v in pts.items() if v}
        ss = {u: dict(v) for u, v in stats.items() if v}
        snap = await plugin._load_daily_snapshot(saves, FileNode())
        out.append(await plugin._compute_daily_points(
            saves, cs, ss, rnd.choice([0, 6]), FileNode(), filename, {},
            {u: "N" + u[-1] for u in ids}, set(cs) | set(ss), snap))
    files = {}
    for name in ("daily_snapshot.json", "daily_history.json"):
        path = os.path.join(saves, ".fhc", "fhr_" + name)
        files[name] = open(path, encoding="utf-8").read() if os.path.exists(path) else ""
    return out, files


def test_daily_golden(tmp_path, fake_clock):
    results = {}
    for seed in range(8):
        saves = tmp_path / f"s{seed}"
        (saves / ".fhc").mkdir(parents=True)
        out, files = asyncio.run(_scenario(str(saves), seed, fake_clock))
        assert files["daily_history.json"], "a week of play should close some days"
        results[f"seed={seed}"] = digest([out, files])
    check_golden("daily.json", results)


def test_persist_false_writes_nothing(tmp_path, fake_clock):
    fake_clock.now = real_datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)
    (tmp_path / ".fhc").mkdir()
    snap_file = tmp_path / ".fhc" / "daily_snapshot.json"
    snap_file.write_text('{"date": "2026-09-01", "snapshot": {"x": 1}}')
    plugin = make_plugin()
    snap = asyncio.run(plugin._load_daily_snapshot(str(tmp_path), FileNode()))
    asyncio.run(plugin._compute_daily_points(str(tmp_path), {"x": 50}, {}, 0, FileNode(), "f.lua",
                                             {}, {}, {"x"}, snap, persist=False))
    assert snap_file.read_text() == '{"date": "2026-09-01", "snapshot": {"x": 1}}'
    assert not (tmp_path / ".fhc" / "fhr_daily_history.json").exists()
    assert not (tmp_path / ".fhc" / "fhr_daily_snapshot.json").exists()
