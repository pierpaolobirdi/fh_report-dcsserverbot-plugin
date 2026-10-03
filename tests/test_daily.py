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


# ── daily reset time zone (the Scheduler plugin's `timezone`) ──────────────────

MADRID = commands._tz_from_name("Europe/Madrid")


def test_time_zone_names():
    from datetime import timezone as tzmod
    assert commands._tz_from_name(None) is tzmod.utc
    assert commands._tz_from_name("") is tzmod.utc
    assert commands._tz_from_name("gmt") is tzmod.utc
    assert str(commands._tz_from_name("Europe/Madrid")) == "Europe/Madrid"
    assert commands._tz_from_name("Nowhere/Land") is tzmod.utc          # warns, never crashes


def _plugin_with_scheduler(config):
    plugin = make_plugin()

    def get_config(server, plugin_name=None, **_):
        assert plugin_name == "scheduler"
        if isinstance(config, Exception):
            raise config
        return config
    plugin.get_config = get_config
    return plugin


def test_the_reset_zone_comes_from_the_scheduler_plugin_or_falls_back_to_utc():
    from datetime import timezone as tzmod
    assert str(_plugin_with_scheduler({"timezone": "Europe/Madrid"})._reset_tz(object())) == "Europe/Madrid"
    assert _plugin_with_scheduler({})._reset_tz(object()) is tzmod.utc                      # no timezone set
    assert _plugin_with_scheduler(ValueError('Plugin "scheduler" not found!'))._reset_tz(object()) is tzmod.utc
    assert _plugin_with_scheduler({"timezone": "Bad/Zone"})._reset_tz(object()) is tzmod.utc


def test_day_start_in_a_local_timezone_follows_summer_and_winter_time():
    cfg = {"daily_reset_hour": 6}
    # 3 Oct (summer time, UTC+2): 06:00 Madrid = 04:00 UTC
    after = real_datetime(2026, 10, 3, 9, 0, tzinfo=timezone.utc)
    assert commands._day_start(cfg, after, MADRID) == real_datetime(2026, 10, 3, 4, 0, tzinfo=timezone.utc)
    before = real_datetime(2026, 10, 3, 3, 0, tzinfo=timezone.utc)                  # still the previous period
    assert commands._day_start(cfg, before, MADRID) == real_datetime(2026, 10, 2, 4, 0, tzinfo=timezone.utc)
    # 3 Nov (winter time, UTC+1): the same 06:00 Madrid is 05:00 UTC
    winter = real_datetime(2026, 11, 3, 9, 0, tzinfo=timezone.utc)
    assert commands._day_start(cfg, winter, MADRID) == real_datetime(2026, 11, 3, 5, 0, tzinfo=timezone.utc)
    # without a zone nothing changes
    assert commands._day_start(cfg, after) == real_datetime(2026, 10, 3, 6, 0, tzinfo=timezone.utc)


def test_schedule_weekdays_are_the_ones_of_the_timezone():
    cfg = {"daily_reset_hour": 6, "daily_reset_schedule": {"sun": 9}}
    auckland = commands._tz_from_name("Pacific/Auckland")
    # Sat 3 Oct 21:00 UTC is already Sunday 4 Oct 10:00 in Auckland (UTC+13): Sunday's 9 applies
    now = real_datetime(2026, 10, 3, 21, 0, tzinfo=timezone.utc)
    assert commands._day_start(cfg, now, auckland) == real_datetime(2026, 10, 3, 20, 0, tzinfo=timezone.utc)


def test_the_daily_counters_roll_over_at_local_midnight_not_utc_midnight(tmp_path, fake_clock):
    saves = str(tmp_path)
    (tmp_path / ".fhc").mkdir()
    plugin = make_plugin()
    ids = UCID[:2]

    async def step(hours_from, tz):
        fake_clock.now = real_datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc) + timedelta(hours=hours_from)
        snap = await plugin._load_daily_snapshot(saves, FileNode())
        pts = {u: 100 * (hours_from + 1) for u in ids}
        return await plugin._compute_daily_points(saves, pts, {}, 0, FileNode(), "f.lua", {}, {}, set(pts), snap, tz=tz)

    async def run(tz):
        await step(0, tz)                       # 12:00 UTC, 14:00 Madrid
        before = (await step(9, tz))[0]         # 21:00 UTC, 23:00 Madrid, same day in both
        after = (await step(11, tz))[0]         # 23:00 UTC = 01:00 Madrid next day
        return before, after
    utc_before, utc_after = asyncio.run(run(timezone.utc))
    assert utc_after[ids[0]] > utc_before[ids[0]] > 0                # UTC: still the same day, still accumulating
    (tmp_path / ".fhc" / "fhr_daily_snapshot.json").unlink()
    mad_before, mad_after = asyncio.run(run(MADRID))
    assert mad_before[ids[0]] > 0 and mad_after.get(ids[0], 0) == 0   # Madrid: a new day started, the counter restarted


# ── reset times are HH:MM ──────────────────────────────────────────────────────

@pytest.mark.parametrize("value, expected", [
    ("8:30", (8, 30)), ("08:30", (8, 30)), ("15:30", (15, 30)), (" 6:05 ", (6, 5)), ("0:00", (0, 0)),
    (4, (4, 0)), ("4", (4, 0)), (0, (0, 0)), (23, (23, 0)), ("23:59", (23, 59)),
    (510, (8, 30)), (930, (15, 30)),             # an unquoted 8:30 / 15:30 read by a YAML 1.1 reader
    (None, (0, 0)), ("", (0, 0)),
])
def test_hh_mm_values(value, expected):
    assert commands._parse_hhmm(value) == expected


@pytest.mark.parametrize("bad", ["24:00", "8:60", "ocho", "8:5", "-1", 24, 59, 1440, True, 8.5, "8:30:00"])
def test_invalid_times_fall_back_to_the_default(bad):
    assert commands._parse_hhmm(bad) == (0, 0)
    assert commands._parse_hhmm(bad, (6, 0)) == (6, 0)


def test_today_reset_time_with_minutes_and_a_per_day_override():
    cfg = {"daily_reset_hour": "8:30", "daily_reset_schedule": {"sat": "7:15", "sun": 9}}
    tz = commands._tz_from_name("Europe/Madrid")

    def at(day):
        class _FakeNow(real_datetime):
            @classmethod
            def now(cls, tz_=None):
                return real_datetime(2026, 10, day, 12, 0, tzinfo=timezone.utc)
        return _FakeNow
    import pytest as _p
    mp = _p.MonkeyPatch()
    try:
        mp.setattr(commands, "datetime", at(3))      # Saturday
        assert commands._reset_time_today(cfg, tz) == (7, 15)
        mp.setattr(commands, "datetime", at(4))      # Sunday
        assert commands._reset_time_today(cfg, tz) == (9, 0)
        mp.setattr(commands, "datetime", at(5))      # Monday: the default
        assert commands._reset_time_today(cfg, tz) == (8, 30)
    finally:
        mp.undo()


def test_day_start_honours_the_minutes():
    cfg = {"daily_reset_hour": "8:30"}
    before = real_datetime(2026, 10, 3, 8, 29, tzinfo=timezone.utc)
    after = real_datetime(2026, 10, 3, 8, 31, tzinfo=timezone.utc)
    assert commands._day_start(cfg, before) == real_datetime(2026, 10, 2, 8, 30, tzinfo=timezone.utc)
    assert commands._day_start(cfg, after) == real_datetime(2026, 10, 3, 8, 30, tzinfo=timezone.utc)


def test_the_daily_counters_roll_over_at_08_30_not_at_08_00(tmp_path, fake_clock):
    saves = str(tmp_path)
    (tmp_path / ".fhc").mkdir()
    plugin = make_plugin()
    ids = UCID[:2]

    async def step(minute, tag):
        fake_clock.now = real_datetime(2026, 10, 3, 6, 0, tzinfo=timezone.utc) + timedelta(minutes=minute)
        snap = await plugin._load_daily_snapshot(saves, FileNode())
        pts = {u: 100 + minute for u in ids}
        return (await plugin._compute_daily_points(saves, pts, {}, (8, 30), FileNode(), "f.lua", {}, {}, set(pts), snap))[0]

    async def run():
        await step(0, "06:00")                # first look: day starts here
        await step(1440 + 0, "next day 06:00")    # the next day, before the reset time
        at_0815 = await step(1440 + 135, "08:15")
        at_0845 = await step(1440 + 165, "08:45")
        return at_0815, at_0845
    at_0815, at_0845 = asyncio.run(run())
    assert at_0815[ids[0]] > 0                # 08:15, before 08:30: still the old period, accumulating
    assert at_0845.get(ids[0], 0) == 0        # 08:45, after 08:30: a new period started
