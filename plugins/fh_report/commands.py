"""
Fh_Report Plugin for DCSServerBot
Reads Foothold campaign save files and posts/updates a Discord embed
with front-line status and pilot leaderboard. No database required.
"""

from __future__ import annotations

import asyncio
import calendar
import functools
import importlib.util
import glob
import json
import logging
import os
import re
import tempfile
import time
from datetime import datetime, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo
from typing import Type

import discord
from discord import app_commands
from discord.ext import tasks
from core import Plugin, TEventListener, utils, Status, Group, Server
from services.bot import DCSServerBot


from .version import __version__

log = logging.getLogger(__name__)

# Shown in every embed footer — bumped manually alongside each GitHub
# release, independent of version.py (which DCSSB manages/reads on its own
# terms; keeping this separate avoids the conflicts that caused).
FH_REPORT_RELEASE = "14.2.12"

# ── Rank thresholds from Foothold engine (zoneCommander.lua) ─────────────────
RANK_THRESHOLDS = [0, 3000, 5000, 8000, 12000, 16000, 22000, 30000, 45000, 65000,
                   90000, 120000, 155000, 195000, 240000, 290000, 345000, 405000, 470000, 540000]
RANK_NAMES = [
    "Recruit", "Aviator", "Airman", "Senior Airman",
    "Staff Sergeant", "Technical Sergeant", "Master Sergeant",
    "Senior Master Sergeant", "Chief Master Sergeant",
    "Second Lieutenant", "First Lieutenant",
    "Captain", "Major", "Lieutenant Colonel", "Colonel",
    "Brigadier General", "Major General", "Lieutenant General",
    "General", "General of the Air Force"
]

# ── CAREER_STAT IDs confirmed from Foothold's zoneCommander.lua source ───────
CAREER_FLIGHT_SECONDS = 1
CAREER_HELO_SECONDS   = 3
CAREER_TRAPS          = 8
CAREER_KILLS          = 10
CAREER_DEATHS         = 21
CAREER_FUEL_LBS       = 30

HOT_STATES = {Status.RUNNING, Status.PAUSED}


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _do_script(server, lua: str) -> None:
    await server.send_to_dcs({"command": "do_script", "script": lua})


# ── Lua table helpers ─────────────────────────────────────────────────────────
# Foothold writes EVERY string (player names, keys) with string.format('%q'): double
# quotes, a '"' inside becomes \", a backslash becomes \\, a newline becomes backslash +
# newline, and an apostrophe stays as is. So a quoted string is matched with its
# escapes, never "up to the next quote of any kind".

_LUA_STR = r'"[^"\\]*(?:\\[\s\S][^"\\]*)*"|\'[^\'\\]*(?:\\[\s\S][^\'\\]*)*\''
_LUA_TOKEN = re.compile(r"[{}]|" + _LUA_STR)                 # a brace, or a whole string (skipped)
_LUA_KEY = re.compile(r"\[\s*(" + _LUA_STR + r")\s*\]\s*=\s*\{")
_LUA_NUM = re.compile(r"\[\s*(" + _LUA_STR + r")\s*\]\s*=\s*(-?\d+(?:\.\d+)?)")
_UCID_PAIR = re.compile(r"\[[\'\"]([a-f0-9]{32})[\'\"]\]\s*=\s*(" + _LUA_STR + ")")
_LUA_ESCAPES = {"n": "\n", "r": "\r", "t": "\t", "a": "\a", "b": "\b", "f": "\f", "v": "\v"}


def _lua_unquote(token: str) -> str:
    """The text of a quoted Lua string token (quotes and escapes included)."""
    body = token[1:-1]
    if "\\" not in body:
        return body

    def one(m):
        c = m.group(1)
        return chr(int(c)) if c.isdigit() else _LUA_ESCAPES.get(c, c)
    return re.sub(r"\\(\d{1,3}|[\s\S])", one, body)


def _lua_quote(text: str) -> str:
    """text as Lua's string.format('%q') writes it."""
    return '"' + (text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\\n")
                  .replace("\r", "\\r").replace("\0", "\\000")) + '"'


def _lua_block(text: str, brace_pos: int) -> tuple[str, int]:
    """`brace_pos` is the index of a '{' in `text`. Returns (inner content,
    index just past the matching '}'). Braces inside quoted strings (a player
    named "Joe :}") don't count; indentation and nesting don't matter."""
    depth = 1
    for m in _LUA_TOKEN.finditer(text, brace_pos + 1):
        tok = m.group()
        if tok == "{":
            depth += 1
        elif tok == "}":
            depth -= 1
            if depth == 0:
                return text[brace_pos + 1:m.start()], m.end()
    return text[brace_pos + 1:], len(text)               # unbalanced (a half-written file)


def _lua_table(text: str, pattern: str) -> str | None:
    """Inner content of the first table whose opening matches `pattern`
    (a regex ending in `\\{`), or None if not found."""
    m = re.search(pattern, text)
    if not m:
        return None
    return _lua_block(text, m.end() - 1)[0]


def _lua_entries(block: str, ucid_only: bool = False):
    """Yield (key, inner) for each top-level `["key"]={...}` entry of `block`
    (either quote style, escapes decoded). ucid_only keeps only 32-hex UCID keys."""
    pos = 0
    while True:
        m = _LUA_KEY.search(block, pos)
        if not m:
            return
        inner, pos = _lua_block(block, m.end() - 1)
        key = _lua_unquote(m.group(1))
        if not ucid_only or _UCID_RE.fullmatch(key):
            yield key, inner


def _find_entry(text: str, key: str):
    """The match of the first `["key"]={` whose decoded key is `key`, or None."""
    for m in _LUA_KEY.finditer(text):
        if _lua_unquote(m.group(1)) == key:
            return m
    return None


def _lua_field_str(block: str, field: str) -> str | None:
    """The string assigned to `["field"]=` in `block` (decoded), or None."""
    q = re.escape(field)
    m = re.search(r"\[\s*(?:\"" + q + r"\"|\'" + q + r"\')\s*\]\s*=\s*(" + _LUA_STR + ")", block)
    return _lua_unquote(m.group(1)) if m else None


def _lua_numbers(block: str, skip: tuple[str, ...] = ()) -> dict:
    """{key: int|float} for every quoted-key numeric assignment in `block`."""
    out = {}
    for m in _LUA_NUM.finditer(block):
        key, val = _lua_unquote(m.group(1)), m.group(2)
        if key not in skip:
            out[key] = float(val) if "." in val else int(val)
    return out


def _ucid_names(text: str) -> list[tuple[str, str]]:
    """(ucid, name) for every `["<ucid>"]="name"` pair (the ucidToName tables)."""
    return [(m.group(1), _lua_unquote(m.group(2))) for m in _UCID_PAIR.finditer(text)]


def _lua_num_str(value: float) -> str:
    return str(int(value)) if float(value) == int(value) else str(value)


_RANKS_PLAYERS_RE  = r"RankSave\[[\"']players[\"']\]\s*=\s*\{"
_PLAYER_STATS_RE   = r"zonePersistance\[[\"']playerStats[\"']\]\s*=\s*\{"


def _ranks_version(content: str) -> int | None:
    m = re.search(r'RankSave\[["\']playerIdentityVersion["\']\]\s*=\s*(\d+)', content)
    return int(m.group(1)) if m else None


def _stats_version(content: str) -> int | None:
    m = re.search(r'zonePersistance\[["\']playerStatsIdentityVersion["\']\]\s*=\s*(\d+)', content)
    return int(m.group(1)) if m else None


class _UpdateReadCache:
    """Memoizes read_file() for one update cycle (or one slash command) so
    several parsers reading the same file only fetch it once from the node.
    Never outlives that scope; call invalidate() after writing a cached path.
    """

    def __init__(self, node):
        self._node = node
        self._files: dict[str, bytes] = {}

    async def read_file(self, path: str) -> bytes:
        if path not in self._files:
            self._files[path] = await self._node.read_file(path)
        return self._files[path]

    def invalidate(self, path: str) -> None:
        self._files.pop(path, None)

    def __getattr__(self, name):
        # list_directory, create_directory, remove_file... go straight to the node
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._node, name)


def _validated_update_interval(raw: dict, default: int = 300) -> tuple[int, str | None]:
    """DEFAULT.update_interval, falling back to `default` (with a warning
    message for the caller to log) if it's non-numeric or <= 0 — such a
    value would otherwise spin the updater loop flat-out or crash it.
    """
    configured = (raw.get("DEFAULT") or {}).get("update_interval", default)
    try:
        interval = int(configured)
    except (TypeError, ValueError):
        return default, (
            f"Fh_Report: DEFAULT.update_interval ({configured!r}) is not a valid "
            f"integer — falling back to the default of {default}s."
        )
    if interval <= 0:
        return default, (
            f"Fh_Report: DEFAULT.update_interval ({interval}) must be greater than "
            f"zero — falling back to the default of {default}s."
        )
    return interval, None


def _parse_interval(value) -> int | None:
    """A usable update_interval (a whole number of seconds > 0), else None."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


_interval_warned: set[str] = set()


def _server_stagger_seconds(interval: float, server_count: int) -> float:
    """Small delay between processing each configured server in one update
    cycle, so multiple servers don't all hit the node/API at once. Capped at
    5s, and scales down as server_count grows. A single-server setup (the
    common case) always gets 0 — no behavior change."""
    if server_count <= 1:
        return 0
    return min(5, interval / server_count)


def _fmt_career_time(seconds: float) -> str:
    """Format seconds as 'Xh Ym' for display (report command uses full form)."""
    total_min = int(seconds) // 60
    h, m = divmod(total_min, 60)
    if h > 0:
        return f"{h}h {m}m" if m else f"{h}h"
    return f"{m}m"


def _fmt_num(v: float) -> str:
    return f"{int(v):,}" if float(v) == int(v) else f"{v:,.2f}"


def _slot_display_counts(total_slots: int, active_slots: int) -> tuple[int, int]:
    """(display_total, display_active) for the 5 drawable slot symbols.
    Up to 5 real slots map 1:1. Above that, the active count is shown as a
    proportion of 5 (round-half-up), so 6/9 and 9/9 don't both look full.
    """
    if total_slots <= 5:
        display_total  = total_slots
        display_active = min(active_slots, total_slots)
        return display_total, display_active

    display_total = 5
    ratio = active_slots / total_slots if total_slots > 0 else 0
    # Round-half-up (not Python's round(), which rounds .5 to even)
    display_active = int(ratio * 5 + 0.5)
    display_active = max(0, min(5, display_active))
    return display_total, display_active


def get_rank(credits: float) -> str:
    rank_idx = 0
    for i, threshold in enumerate(RANK_THRESHOLDS):
        if credits >= threshold:
            rank_idx = i
        else:
            break
    return RANK_NAMES[rank_idx]


def _node_listing(listing) -> tuple[list[str], bool]:
    """(basenames, newest_first) from a node.list_directory() result. DCSSB
    returns (directory, [full paths]) ordered newest first; older versions
    returned plain names, in no particular order."""
    if isinstance(listing, tuple) and len(listing) == 2:
        return [os.path.basename(str(p).replace("\\", "/")) for p in listing[1]], True
    return [os.path.basename(str(p).replace("\\", "/")) for p in listing], False


async def _dir_exists(node, path: str) -> bool | None:
    """Whether `path` is an existing folder on the node; None if that can't be
    told. list_directory() lists nothing for a missing folder instead of failing,
    so ask the parent folder for a folder of that name."""
    parent, name = os.path.split(path.rstrip("/\\"))
    if not name:
        return None
    try:
        names, _ = _node_listing(await node.list_directory(parent, pattern=glob.escape(name), is_dir=True))
    except Exception:
        return None
    return any(n.lower() == name.lower() for n in names)


async def _ensure_fhc_dir(node, saves_dir: str) -> None:
    """Create saves_dir/.fhc on the node, local or remote: write_file() doesn't
    create folders. Not done while saves_dir itself is missing (the Foothold
    mission never ran there)."""
    if await _dir_exists(node, saves_dir) is False:
        return
    fhc = os.path.join(saves_dir, ".fhc")
    try:
        if hasattr(node, "create_directory"):
            await node.create_directory(fhc)
        elif os.path.isdir(saves_dir):          # older DCSSB: local nodes only
            os.makedirs(fhc, exist_ok=True)
    except Exception as e:
        log.debug(f"Fh_Report: could not create {fhc}: {e}")


async def _remove_file(node, path: str) -> None:
    """Best-effort delete through the node (remote nodes too). DCSSB's
    remove_file() takes a glob pattern, hence the escape. Older DCSSB without
    it: local delete only."""
    try:
        if hasattr(node, "remove_file"):
            await node.remove_file(glob.escape(path))
        elif os.path.isfile(path):
            os.remove(path)
    except Exception as e:
        log.debug(f"Fh_Report: could not remove {path}: {e}")


async def find_persistence_file(saves_dir: str, node) -> str | None:
    """Active Foothold save: the file named in foothold.status (basename
    only — the stored path may be a Windows path invalid on this host),
    else the most recently modified foothold_*.lua in saves_dir. Reads go
    through `node` so remote agent nodes work.
    """
    status_file = os.path.join(saves_dir, "foothold.status")
    try:
        data = await node.read_file(status_file)
        # Extract only the filename from the path stored in foothold.status.
        # The full path is irrelevant — it may be a Windows absolute path that
        # is invalid on Linux/Wine hosts. The file always lives in saves_dir.
        raw_path = data.decode("utf-8").strip()
        filename = os.path.basename(raw_path)
        path     = os.path.join(saves_dir, filename)
        try:
            await node.read_file(path)
            return path
        except OSError:
            # FileNotFoundError or TimeoutError (remote RPC read timed out) — both
            # OSError: fall back to the directory listing below.
            pass
    except OSError:
        pass
    # Fallback: list directory and find foothold_*.lua candidates
    try:
        names, newest_first = _node_listing(await node.list_directory(saves_dir))
        candidates = [
            n for n in names
            if n.lower().startswith("foothold_") and n.lower().endswith(".lua") and "rank" not in n.lower()
        ]
        if not candidates:
            return None
        return os.path.join(saves_dir, candidates[0] if newest_first else sorted(candidates)[-1])
    except Exception:
        return None


_ZONE_NAME_RE = re.compile(r"zonePersistance\[[\"']zones[\"']\]\[\s*(" + _LUA_STR + r")\s*\]")
_ZONE_BLOCK_RE = re.compile(
    r"zonePersistance\[[\"']zones[\"']\]\[\s*(" + _LUA_STR + r")\s*\] = \{(.*?)(?=\nzonePersistance|\Z)", re.DOTALL)


async def parse_zones(filepath: str, node) -> dict:
    """Parse zone persistence file. Returns {'blue': [...], 'red': [...]}."""
    data = await node.read_file(filepath)
    content = data.decode("utf-8", errors="replace")

    zones = {"blue": [], "red": [], "neutral": 0}
    zone_names = [_lua_unquote(t) for t in _ZONE_NAME_RE.findall(content)]

    # BLUE-only extra slots (ZoneCommander:addExtraSlot): +1 per zone, +1 more
    # when globalExtraUnlock is true. RED has its own mechanic, not handled here.
    gxu_m = re.search(r'globalExtraUnlock"?\]\s*=\s*(true|false)', content)
    global_extra_unlock = (gxu_m.group(1) == "true") if gxu_m else False

    # One pass over the file: first ` = {` block per zone, up to the next
    # top-level zonePersistance line.
    zone_blocks: dict[str, str] = {}
    for m in _ZONE_BLOCK_RE.finditer(content):
        zone_blocks.setdefault(_lua_unquote(m.group(1)), m.group(2))

    for zone in zone_names:
        block = zone_blocks.get(zone)
        if block is None:
            continue

        side_m      = re.search('\\[(?:"side"|\'side\')\\]=(\\d+)', block)
        active_m    = re.search('\\[(?:"active"|\'active\')\\]=(true|false)', block)
        level_m     = re.search('\\[(?:"level"|\'level\')\\]=(\\d+)', block)
        suspended_m = re.search('\\[(?:"suspended"|\'suspended\')\\]=(true|false)', block)

        if not side_m:
            continue

        side      = int(side_m.group(1))
        active    = active_m.group(1) == "true" if active_m else False
        level     = int(level_m.group(1)) if level_m else 0
        suspended = suspended_m.group(1) == "true" if suspended_m else False

        if not active or level == 0:
            continue
        # Skip hidden/internal zones
        if zone.lower().startswith("hidden"):
            continue

        # Neutral zones — count for bar but don't list
        if side == 0:
            zones["neutral"] += 1
            continue

        # Count active slots using Foothold's logic (mirrors IsSavedUnitSlotEmpty):
        # A slot [N] is active if it contains at least one unit name string.
        # We find the remainingUnits block then count non-empty slot entries.
        active_slots = 0
        ru_key = '"remainingUnits"' if '"remainingUnits"' in block else "'remainingUnits'"
        ru_start = block.find(f'[{ru_key}]={{')
        if ru_start != -1:
            ru_block, _ = _lua_block(block, block.find('{', ru_start))
            # Count active slots across ALL slots (1..level), not just the
            # first 5, so zones with live slots beyond position 5 still show
            # as having active defenses (display is capped at 5 symbols).
            for idx in range(1, level + 1):
                slot_key = f'[{idx}]={{'
                sk = ru_block.find(slot_key)
                if sk == -1:
                    continue
                slot_content, _ = _lua_block(ru_block, sk + len(slot_key) - 1)
                # Active if any quoted non-empty string inside
                if re.search(r'["\x27][^"\x27]{1,}["\x27]', slot_content):
                    active_slots += 1

        info = {"name": zone, "level": level, "active_slots": active_slots, "suspended": suspended}
        if side == 2:
            # BLUE true max = randomUpgradesBlue entries + extra-slot allowance. Can
            # exceed `level` (unlocked but unbuilt slots draw as empty symbols).
            ru_key2 = '"randomUpgradesBlue"' if '"randomUpgradesBlue"' in block else "'randomUpgradesBlue'"
            rub_start = block.find(f'[{ru_key2}]={{')
            base_slots = 0
            if rub_start != -1:
                rub_block, _ = _lua_block(block, block.find('{', rub_start))
                base_slots = len(re.findall(r'\[\d+\]=', rub_block))
            extra_allowance = 2 if global_extra_unlock else 1
            info["true_max"] = base_slots + extra_allowance
            zones["blue"].append(info)
        elif side == 1:
            zones["red"].append(info)

    return zones


async def parse_player_stats(filepath: str, node) -> tuple[dict, dict, dict]:
    """Parse playerStats from a Foothold save. Returns
    (campaign_stats {name: Points}, session_stats_raw {name: {stat: value}}
    without Points, name_to_ucid {name: ucid}).

    zonePersistance["playerStatsIdentityVersion"] selects the format:
      nil -> old name-keyed format (UCIDs only from an optional ucidToName)
      1   -> 4.9.1+ UCID-keyed format with "name" and a nested "stats" table
      other -> unknown future format: skipped, warned once.
    """
    try:
        data = await node.read_file(filepath)
        content = data.decode("utf-8", errors="replace")

        version = _stats_version(content)

        if version is not None and version != 1:
            if filepath not in _unsupported_version_warned:
                _unsupported_version_warned.add(filepath)
                log.warning(
                    f"Fh_Report: {filepath} reports playerStatsIdentityVersion={version}, "
                    f"which this version of Fh_Report doesn't understand yet — "
                    f"please update Fh_Report. Skipping this file for now."
                )
            return {}, {}, {}

        block = _lua_table(content, _PLAYER_STATS_RE)
        if block is None:
            return {}, {}, {}
        results = {}
        raw_all = {}
        name_to_ucid: dict = {}

        # Old format: name-keyed, stats inline in the player block.
        # New format (4.9.1+): UCID-keyed, "name" + nested "stats" table.
        for key, player_block in _lua_entries(block, ucid_only=version is not None):
            if version is None:
                name, stats_block = key, player_block
            else:
                name = _lua_field_str(player_block, "name")
                if not name:
                    continue
                stats_block = _lua_table(player_block, r'\[(?:"stats"|\'stats\')\]\s*=\s*\{')
                if stats_block is None:
                    continue
            pts_m = re.search(r'\[(?:"Points"|\'Points\')\]\s*=\s*(\d+)', stats_block)
            if not pts_m:
                continue
            results[name] = int(pts_m.group(1))
            raw_all[name] = _lua_numbers(stats_block, skip=("Points",))
            if version is not None:
                name_to_ucid[name] = key

        if version is None:
            # Native ucidToName may still be present as a supplementary
            # table even in an old-format file (an interim Foothold step) —
            # use it opportunistically if so, costs nothing if absent.
            ucid_block = _lua_table(content, r"zonePersistance\[[\"']ucidToName[\"']\]\s*=\s*\{")
            if ucid_block:
                for ucid, name in _ucid_names(ucid_block):
                    name_to_ucid[name] = ucid

        return results, raw_all, name_to_ucid
    except Exception:
        return {}, {}, {}


async def hot_write_waypoints(server) -> None:
    """Inject Lua that dumps the mission's in-memory WaypointList (zone ->
    waypoint suffix, never saved by Foothold) to saves_dir/.fhc/fhc_waypoints.lua.
    Same file and technique as Fh_Control, so either plugin can refresh it.
    No-op if the mission doesn't define WaypointList.
    """
    lua = (
        "if WaypointList and lfs and io then "
        "  lfs.mkdir(lfs.writedir() .. [[Missions/Saves/.fhc]]) "
        "  local _p = lfs.writedir() .. [[Missions/Saves/.fhc/fhc_waypoints.lua]] "
        "  local _f = io.open(_p, 'w') "
        "  if _f then "
        "    _f:write([[-- Fh_Report/Fh_Control waypoint cache\n]]) "
        "    _f:write([[WaypointList = {\n]]) "
        "    for _k,_v in pairs(WaypointList) do "
        "      _f:write([[  [\"]] .. _k .. [[\"] = \"]] .. _v .. [[\",\n]]) "
        "    end "
        "    _f:write([[}\n]]) "
        "    _f:close() "
        "  end "
        "end"
    )
    await _do_script(server, lua)


async def load_waypoint_list(saves_dir: str, node) -> dict:
    """{zone_name: waypoint number} from fhc_waypoints.lua (written by us or
    Fh_Control). Zones without a numeric suffix are omitted; a missing or
    unreadable file returns {}.
    """
    path = os.path.join(saves_dir, ".fhc", "fhc_waypoints.lua")
    try:
        raw = (await node.read_file(path)).decode("utf-8", errors="ignore")
    except Exception:
        return {}
    result: dict[str, int] = {}
    for m in re.finditer(r'\["([^"]+)"\]\s*=\s*"([^"]*)"', raw):
        zone_name, suffix = m.group(1), m.group(2)
        num_match = re.search(r"(\d+)", suffix)
        if num_match:
            result[zone_name] = int(num_match.group(1))
    return result


# ── Campaign session start ────────────────────────────────────────────────────
# "Session" is the life of the Foothold campaign (the Session Leaderboard). It
# starts when the campaign is reset: Foothold empties the SAME save file and
# restarts the mission (so the file's creation date means nothing), or an admin
# deletes the tracking files, or the map changes (another save file). Nothing
# stores the start, so a reset is noticed and dated here. It is only used to
# count things since then (BoB), never shown.
#   - save file name changed / save reappeared after "campaign not started":
#     a new session at once.
#   - save reset in place (every known player gone, or points and kills both
#     dropped): confirmed on a second look one cycle later, so a read caught
#     while Foothold is rewriting the file can't restart the session.
#   - the start is dated at the mission start DCSServerBot recorded since the
#     campaign was last seen intact (Foothold reloads the mission right after a
#     reset), or the moment it was noticed.
# State (start, last seen intact, a small baseline of players) lives in
# .fhc/fhr_session.json, written by the updater only.

_SESSION_ISO = "%Y-%m-%dT%H:%M:%S"
_SESSION_WINDOW = timedelta(hours=1)    # widest "last seen intact -> noticed" gap still narrowed
_SESSION_SEEN_EVERY = timedelta(minutes=15)   # how often the baseline is written to disk


def _session_migrate(old: dict) -> dict:
    """fhr_session.json written by 14.1.28 (creation-time probe) -> this format."""
    if not old or old.get("kind") in ("detected", "first_seen"):
        return old
    kind = old.get("fallback_kind") or "first_seen"
    start = old.get("fallback") or old.get("start")
    return {"file": old.get("file", ""), "start": start, "kind": kind, "seen": start} if start else {}


def _session_start_for(st: dict, cfg: dict, tz: tzinfo = timezone.utc) -> datetime | None:
    """Session start to count from. A campaign already running when Fh_Report
    first looked ("first_seen") started at some unknown earlier time, so it
    counts at least from the daily reset before that first look: the session
    includes the day, as the Session Leaderboard does. A detected reset counts
    from the reset."""
    start = _parse_utc((st or {}).get("start"))
    if start and st.get("kind") == "first_seen":
        start = min(start, _day_start(cfg, start.replace(tzinfo=timezone.utc), tz).replace(tzinfo=None))
    return start


def _campaign_looks_reset(prev: dict | None, cur: dict) -> bool:
    """Has the campaign been reset in place between two readings? `prev` and
    `cur` are {"points": {id: points}, "kills": {id: Air+Ground kills}}.
    True when nobody known is left, or points AND kills both fell for the
    players present in both (the rule the daily counter uses too)."""
    if not prev or not prev.get("points"):
        return False
    common = set(prev["points"]) & set(cur["points"])
    if not common:
        return sum(prev["points"].values()) > 0
    prev_pts = sum(prev["points"][i] for i in common)
    cur_pts = sum(cur["points"][i] for i in common)
    prev_kills = sum(prev["kills"].get(i, 0) for i in common)
    cur_kills = sum(cur["kills"].get(i, 0) for i in common)
    return prev_pts > 0 and cur_pts < prev_pts * 0.5 and prev_kills > 0 and cur_kills < prev_kills


_unsupported_version_warned: set[str] = set()
def _node_name(server) -> str:
    """Name of the node hosting `server` ("" when it has none, as some test stand-ins)."""
    return getattr(getattr(server, "node", None), "name", "") or ""


def _server_key(server) -> str:
    """Identifies a server across the cluster: `node/instance`. Instance names
    are only unique per node, so the instance name alone is not enough."""
    node = _node_name(server)
    return f"{node}/{server.instance.name}" if node else server.instance.name


def _saves_key(node, saves_dir: str) -> str:
    """A Saves folder on a given node (the same path can exist on several)."""
    name = getattr(node, "name", "") or ""
    return f"{name}:{saves_dir}" if name else saves_dir


_unmatched_instance_warned: set[str] = set()
_ambiguous_warned: set[str] = set()
_missing_save_warned: set[str] = set()       # instances already warned about a missing Foothold save
_unmatched_instance_since: dict[str, float] = {}
_UNMATCHED_INSTANCE_GRACE_SECONDS = 120  # tolerate remote-node startup races


async def parse_ranks(filepath: str, excluded_ucids: list[str], node) -> dict:
    """Parse Foothold_Ranks.lua into {name: {credits, ucid, career}}, sorted
    by credits desc, skipping excluded_ucids.

    RankSave["playerIdentityVersion"] selects the format (not RankSave["version"],
    which tracks career data):
      nil    -> old name-keyed format, UCID via the ucidToName table
      1 or 2 -> 4.9.1+ UCID-keyed format with "name" inside each block
      other  -> unknown future format: skipped, warned once.
    Foothold migrates the file itself once, so per-read detection is enough.
    """
    data = await node.read_file(filepath)
    content = data.decode("utf-8", errors="replace")

    version = _ranks_version(content)

    if version is not None and version not in (1, 2):
        if filepath not in _unsupported_version_warned:
            _unsupported_version_warned.add(filepath)
            log.warning(
                f"Fh_Report: {filepath} reports playerIdentityVersion={version}, "
                f"which this version of Fh_Report doesn't understand yet — "
                f"please update Fh_Report. Skipping this file for now."
            )
        return {}

    players_block = _lua_table(content, _RANKS_PLAYERS_RE)
    if players_block is None:
        return {}

    excluded = set(excluded_ucids or [])
    # Old format: name-keyed, UCID resolved via the separate ucidToName table.
    name_to_ucid = (
        {name: ucid for ucid, name in _ucid_names(content)}
        if version is None else {}
    )

    players = {}
    for key, block in _lua_entries(players_block, ucid_only=version is not None):
        credit_m = re.search(r'\[(?:"credits"|\'credits\')\]\s*=\s*([\d.]+)', block)
        if not credit_m:
            continue
        if version is None:
            clean_name = key.strip()
            ucid = name_to_ucid.get(clean_name)
        else:
            ucid = key
            name = _lua_field_str(block, "name")
            if not name:
                continue
            clean_name = name.strip()
        if len(clean_name) < 2 or (ucid and ucid in excluded):
            continue

        career: dict = {}
        career_block = _lua_table(block, r'\[(?:"career"|\'career\')\]\s*=\s*\{')
        if career_block:
            for cm in re.finditer(r'\[(\d+)\]\s*=\s*([\d.]+)', career_block):
                career[int(cm.group(1))] = float(cm.group(2))

        players[clean_name] = {
            "credits": float(credit_m.group(1)),
            "ucid":    ucid,
            "career":  career,
        }

    return dict(sorted(players.items(), key=lambda x: x[1]["credits"], reverse=True))


# Paths whose write failure was already logged at ERROR (then DEBUG until
# a write to that path succeeds again).
_dedup_write_warned: set[str] = set()

# One-time INFO that DCSServerBot predates node.write_file(target, source,
# overwrite) (3.0.4.28+); the local fallback still works for local nodes.
_old_dcssb_api_warned = False

# Tracks which saves_dir paths have already had their one-time write
# self-test run this bot session, so it only ever runs once per instance
# per process lifetime — not on every update cycle.
_write_self_tested: set[str] = set()


async def write_bytes_to_node(node, target_path: str, data: bytes, log=None) -> bool:
    """Write `data` to `target_path` on `node`, local or remote.
    1. node.write_file(target, source, overwrite) — DCSServerBot 3.0.4.28+,
       the only path that reaches a remote agent node (source is a local temp file).
    2. Fallback: local atomic write (older DCSServerBot, local node only).
    If both fail it's almost certainly a remote node on an old DCSServerBot:
    log one clear hint to update instead of a bare OS error.
    """
    # ── Attempt 1: new-style node.write_file(target, source, overwrite) ──
    tmp_local_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", suffix=".fhrep.tmp", delete=False) as tf:
            tf.write(data)
            tmp_local_path = tf.name
        status = await node.write_file(target=target_path, source=tmp_local_path, overwrite=True)
        if getattr(status, "name", None) == "OK":
            _dedup_write_warned.discard(target_path)
            return True
        elif log:
            log.debug(f"Fh_Report: node.write_file (new API) returned {status!r} for {target_path}")
    except TypeError:
        # Old signature (filename, url, overwrite): pre-3.0.4.28 DCSServerBot.
        global _old_dcssb_api_warned
        if not _old_dcssb_api_warned:
            _old_dcssb_api_warned = True
            if log:
                log.info(
                    "Fh_Report: detected an older DCSServerBot version (older "
                    "than 3.0.4.28) — falling back to local file writes, which "
                    "work fine for local/master-node instances. Update "
                    "DCSServerBot to 3.0.4.28 or later to enable full "
                    "remote-agent-node compatibility for Fh_Report's file-write "
                    "features. This message won't repeat."
                )
    except Exception as e:
        if log:
            log.debug(f"Fh_Report: node.write_file (new API) failed for {target_path}: {e}")
    finally:
        if tmp_local_path:
            try:
                os.remove(tmp_local_path)
            except OSError:
                pass

    # ── Attempt 2: local open()/os.replace() — only reaches local nodes ──
    try:
        tmp = target_path + ".fhrep.tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, target_path)
        _dedup_write_warned.discard(target_path)
        return True
    except Exception as e:
        if target_path not in _dedup_write_warned:
            _dedup_write_warned.add(target_path)
            if log:
                log.error(
                    f"Fh_Report: could not write {target_path} via either the new "
                    f"node.write_file() API or a local write ({e}). If this "
                    f"instance runs on a remote agent node, please update "
                    f"DCSServerBot to 3.0.4.28 or later (currently on the 'dev' "
                    f"branch as of writing) — it adds the remote file-write "
                    f"support Fh_Report needs for this. This message won't repeat "
                    f"until the write succeeds, or fails again after that."
                )
        else:
            if log:
                log.debug(f"Fh_Report: write to {target_path} failed again: {e}")
        return False


async def _read_json(node, path: str) -> dict:
    """Read a JSON file through `node` (works for remote agent nodes too).
    Returns {} if missing or unreadable."""
    try:
        return json.loads((await node.read_file(path)).decode("utf-8"))
    except Exception:
        return {}


MAP_PROBE_TIMEOUT = 15   # seconds to wait for DCSSB to read the map from the mission file

FHR_PREFIX = "fhr_"    # files this plugin keeps in saves_dir/.fhc (Fh_Control uses fhc_)


def _fhr_path(saves_dir: str, name: str) -> str:
    return os.path.join(saves_dir, ".fhc", FHR_PREFIX + name)


_legacy_cleaned: set[str] = set()      # old files already dealt with this run


async def _remove_legacy_file(node, path: str) -> None:
    """Delete an old, un-prefixed file once its fhr_ replacement is confirmed.
    Once per file and run (a remote delete is an RPC); a file that is already
    gone is simply a no-op."""
    tag = _saves_key(node, path)
    if tag in _legacy_cleaned:
        return
    _legacy_cleaned.add(tag)
    await _remove_file(node, path)
    log.debug(f"Fh_Report: legacy file {path} cleaned up (replaced by its {FHR_PREFIX} copy)")


async def _read_fhr_json(node, saves_dir: str, name: str, cleanup: bool = False) -> dict:
    """Read saves_dir/.fhc/fhr_<name>, falling back to the old un-prefixed
    <name> (files written before the fhr_ prefix existed). Writers always use the fhr_ name, so an old
    file is read only until the next write creates its fhr_ replacement.
    With cleanup=True (the updater, the only writer), the old file is removed
    the first time the fhr_ file is read back OK — never in the same step that
    wrote it, so a bad write still leaves the old data to fall back on."""
    data = await _read_json(node, _fhr_path(saves_dir, name))
    if data:
        if cleanup:
            await _remove_legacy_file(node, os.path.join(saves_dir, ".fhc", name))
        return data
    return await _read_json(node, os.path.join(saves_dir, ".fhc", name))


async def run_write_self_test(node, saves_dir: str, log=None) -> None:
    """Once per bot session and saves_dir, write/read back/delete a tiny
    test file through write_bytes_to_node, so a write problem shows up at
    startup rather than at the next real correction. If saves_dir doesn't
    exist yet (mission never run) it's logged at DEBUG and retried later;
    any other check failure is inconclusive and the test runs anyway.
    """
    if _saves_key(node, saves_dir) in _write_self_tested:
        return

    if await _dir_exists(node, saves_dir) is False:
        # None = couldn't tell (timeout, permissions...): not assumed missing.
        if log:
            log.debug(
                f"Fh_Report: write self-test skipped for {saves_dir} — the save "
                f"folder doesn't exist yet (mission/server probably hasn't run "
                f"yet). Will retry on a later cycle once it exists."
            )
        return

    _write_self_tested.add(_saves_key(node, saves_dir))

    test_path = os.path.join(saves_dir, "fhrep_write_test.tmp")
    test_content = b"Fh_Report write self-test - safe to delete"

    if log:
        log.debug(f"Fh_Report: running one-time write self-test for {saves_dir}")

    ok = await write_bytes_to_node(node, test_path, test_content, log=log)
    if not ok:
        # write_bytes_to_node already logged the detailed ERROR/INFO hint
        # itself — nothing more to do here.
        return

    try:
        readback = await node.read_file(test_path)
        if readback != test_content:
            if log:
                log.warning(
                    f"Fh_Report: write self-test for {saves_dir} wrote successfully "
                    f"but read back different content than expected — file writes "
                    f"may not be fully reliable for this instance."
                )
        elif log:
            log.debug(f"Fh_Report: write self-test for {saves_dir} passed (write + read-back verified)")
    except Exception as e:
        if log:
            log.debug(f"Fh_Report: write self-test for {saves_dir}: could not read back test file: {e}")

    await _remove_file(node, test_path)


async def deduplicate_ranks(ranks_file: str, persistence_file, node,
                            ranks_source: bytes | None = None) -> bool:
    """Merge duplicate entries in an OLD-format (name-keyed) Foothold_Ranks.lua
    caused by callsign changes. The entry with a UCID in ucidToName is
    canonical: it keeps its whole block (career included), renamed via
    strip_callsign(), with credits summed and lastSeen maxed. The UCID-keyed
    format can't have such duplicates and is skipped.
    Returns True if the file was rewritten.
    """

    if ranks_source is None:
        ranks_source = await node.read_file(ranks_file)
    try:
        original = ranks_source.decode("utf-8")
    except UnicodeDecodeError:
        return False   # never rewrite a file we can't round-trip byte for byte
    if _ranks_version(original) is not None:
        return False
    players_block = _lua_table(original, _RANKS_PLAYERS_RE)
    if players_block is None:
        return False

    credits_re   = r"(\[[\x27\x22]credits[\x27\x22]\]\s*=\s*)([\d.]+)"
    last_seen_re = r"(\[[\x27\x22]lastSeen[\x27\x22]\]\s*=\s*)([\d.]+)"

    entries: dict[str, dict] = {}
    for name, block in _lua_entries(players_block):
        cr_m = re.search(credits_re, block)
        if cr_m and len(name) >= 2:
            ls_m = re.search(last_seen_re, block)
            entries[name] = {
                "credits":  float(cr_m.group(2)),
                "lastSeen": float(ls_m.group(2)) if ls_m else 0.0,
                "block":    block,
            }

    base_to_raws: dict[str, list] = {}
    for raw in entries:
        base_to_raws.setdefault(strip_callsign(raw), []).append(raw)
    duplicates = {b: r for b, r in base_to_raws.items() if len(r) > 1}
    if not duplicates:
        return False

    raw_to_ucid = {name: ucid for ucid, name in _ucid_names(original)}
    ranks_data  = original
    modified    = False

    for base_name, raw_names in duplicates.items():
        names_with_ucid = [n for n in raw_names if n in raw_to_ucid]

        # A rename only if EXACTLY ONE raw name has a live UCID. Several live UCIDs
        # sharing a stripped name (e.g. two "... | 82 TF AA") are different players.
        if len(names_with_ucid) != 1:
            if len(names_with_ucid) > 1:
                log.debug(
                    f"Fh_Report: deduplicate_ranks: '{base_name}' has "
                    f"{len(names_with_ucid)} raw names each with their own "
                    f"live UCID ({names_with_ucid}) — treating as distinct "
                    f"players who share a stripped base name. Skipping merge."
                )
            continue
        name_with_ucid = names_with_ucid[0]

        canonical     = strip_callsign(name_with_ucid)
        ucid          = raw_to_ucid[name_with_ucid]
        total_credits = sum(entries[n]["credits"] for n in raw_names)
        max_last_seen = max(entries[n]["lastSeen"] for n in raw_names)

        # ── Remove each raw entry ─────────────────────────────────────────
        for raw in raw_names:
            m = _find_entry(ranks_data, raw)
            if not m:
                log.warning(f"Fh_Report: deduplicate_ranks: could not find entry for '{raw}' to remove")
                continue
            _, end_pos = _lua_block(ranks_data, m.end() - 1)
            line_start = ranks_data.rfind("\n", 0, m.start())
            start_pos  = line_start + 1 if line_start >= 0 else m.start()
            while end_pos < len(ranks_data) and ranks_data[end_pos] in (",", " ", "\r"):
                end_pos += 1
            if end_pos < len(ranks_data) and ranks_data[end_pos] == "\n":
                end_pos += 1
            ranks_data = ranks_data[:start_pos] + ranks_data[end_pos:]

        # ── Insert canonical entry (keeps career and any other fields) ────
        inner = entries[name_with_ucid]["block"]
        inner = re.sub(credits_re, lambda mm: mm.group(1) + _lua_num_str(total_credits), inner, count=1)
        if re.search(last_seen_re, inner):
            inner = re.sub(last_seen_re, lambda mm: mm.group(1) + _lua_num_str(max_last_seen), inner, count=1)
        else:
            inner = inner.rstrip() + f'\n    ["lastSeen"]={_lua_num_str(max_last_seen)},\n  '
        new_entry  = "  [" + _lua_quote(canonical) + "]={" + inner + "},\n"
        ranks_data = re.sub(r'(RankSave\[[\'\"]players[\'\"]\]\s*=\s*\{)',
                            lambda mm: mm.group(1) + "\n" + new_entry, ranks_data, count=1)

        # ── Update ucidToName ─────────────────────────────────────────────
        for m in _UCID_PAIR.finditer(ranks_data):
            if m.group(1) == ucid and _lua_unquote(m.group(2)) == name_with_ucid:
                ranks_data = ranks_data[:m.start()] + f'["{ucid}"]={_lua_quote(canonical)}' + ranks_data[m.end():]
                break

        modified = True
        log.info(
            f"Fh_Report: merged duplicate entries {raw_names} -> '{canonical}' "
            f"(credits: {total_credits}, lastSeen: {max_last_seen})"
        )

    if not modified:
        return False

    # Re-read right before writing to detect if Foothold itself wrote to
    # this file in the meantime, and skip this cycle's write rather than
    # risk clobbering a newer version — the next cycle will simply retry.
    recheck = (await node.read_file(ranks_file)).decode("utf-8")
    if recheck != original:
        log.warning(
            f"Fh_Report: {ranks_file} changed since read (likely written by "
            f"Foothold) — skipping deduplication this cycle, will retry next."
        )
        return False

    return await write_bytes_to_node(node, ranks_file, ranks_data.encode("utf-8"), log=log)


def _is_numeric_segment(s: str) -> bool:
    """Return True if a name segment is mainly numeric (>49% digits/separators).
    Counts digits, hyphens and underscores as numeric characters.
    Catches slot/squadron identifiers like 307, 305A, A305, 3-7, VFA-75, F_16."""
    s = s.strip()
    if not s:
        return False
    numeric = sum(1 for c in s if c.isdigit() or c in '-_')
    return numeric / len(s) > 0.49


_CALLSIGN_RE = re.compile(r'^[A-Z][A-Z0-9]* \d+[-_]\d+\s*', re.IGNORECASE)


@functools.lru_cache(maxsize=4096)
def strip_callsign(name: str) -> str:
    """Remove the flight callsign prefix from a pilot name.
    Splits on |, /, backslash, comma or ' - ' (keeping the last part) and
    drops a leading 'WORD N-N' / 'WORD N_N' callsign. Squadron tags like
    [MA] are kept. With '|' separators and a mainly numeric last segment
    (slot number), the second-to-last is used: 'GUNSTAR 11 | DRCHOW | 307' -> 'DRCHOW'.
    """
    # Step 1 — handle pipe separators specially
    if '|' in name:
        parts = [p.strip() for p in name.split('|')]
        if len(parts) >= 2 and _is_numeric_segment(parts[-1]):
            # Last segment is a slot number — use second-to-last
            name = parts[-2]
        else:
            # Normal case — use last segment
            name = parts[-1]
    else:
        for sep in ['/', chr(92), ',', ' - ']:
            if sep in name:
                name = name.split(sep)[-1].strip()
                break

    # Step 2 — remove leading callsign pattern: WORD(s) N-N
    # e.g. "UZI 1-1 zarpa" → "zarpa", but not "[MA] Leka" or "132nd Kimkiller"
    stripped = _CALLSIGN_RE.sub('', name).strip()
    # Only apply if result is not empty
    if stripped:
        name = stripped

    return name.strip()


def _safe_code_span(text: str) -> str:
    """Wrap text in a Markdown code span that can't break, whatever it
    contains: the fence is one backtick longer than the longest backtick run
    inside (CommonMark rule), padded with spaces when the text starts/ends
    with a backtick or is blank. Normal names get a plain single-backtick wrap.
    """
    if text is None:
        text = ""
    longest_run = 0
    current = 0
    for ch in text:
        if ch == "`":
            current += 1
            longest_run = max(longest_run, current)
        else:
            current = 0
    fence = "`" * (longest_run + 1)
    if text.startswith("`") or text.endswith("`") or text.strip() == "":
        text = f" {text} "
    return f"{fence}{text}{fence}"


def _fit_rank(prefix: str, rank: str, suffix: str, threshold: int = 78) -> str:
    """Shorten `rank` from the tail (adding '..') just enough to keep the
    visible line (prefix + rank + suffix, Markdown excluded) under
    `threshold` characters on Discord desktop. Works for hook-supplied
    custom ranks too, which a fixed abbreviation table couldn't.
    """
    other_len = len(prefix) + len(suffix)
    full_len  = other_len + len(rank)
    if full_len < threshold:
        return rank
    budget = (threshold - 1) - other_len  # max chars rank may occupy, strictly under threshold
    if budget <= 0:
        return ""  # extreme edge case — no room for any rank text at all
    if budget <= 2:
        return rank[:budget]
    return rank[:budget - 2] + ".."


# (min_points, icon, label, hammer_count)
PUNISHMENT_THRESHOLDS = [
    (200, "💀", "Dishonorably discharged", 6),
    (101, "🔒", "Brig time",               5),
    (51,  "⛓️", "Confined to quarters",    4),
    (26,  "⚖️", "JAG indictment filed",    3),
    (11,  "🔍", "JAG's investigation",    2),
    (1,   "🧿", "JAG's watch",             1),
]

def get_punishment_badge(points: float, name: str = "", custom_icon: str = "",
                         custom_label: str = "", pre_icon: str = "") -> str | None:
    """Returns indented badge line for a given punishment points total, or None."""
    for min_pts, icon, label, hammers in PUNISHMENT_THRESHOLDS:
        if points >= min_pts:
            prefix     = f"`{name}` " if name else ""
            used_pre   = pre_icon if pre_icon else icon
            hammer     = custom_icon if custom_icon else "🔨"
            gravity    = hammer * hammers
            used_label = custom_label if custom_label else label
            pts_str = f"({int(points)} p.p.) "
            return f"·　{used_pre} {prefix}{used_label} {pts_str}{gravity}"
    return None


def _build_podium_table(history: dict, players: dict, days: int, top: int,
                        strip_callsign_flag: bool = False,
                        min3_latest_day: bool = False,
                        name_to_ucid: dict | None = None) -> str | None:
    """Podium text: for each closing event (newest date first), the top `top`
    positions of that day as
        __date__ (Session End)
        🥇 `Name` — **Rank** — N,NNN pts
    (single-position events go on one line). `days` = 0 means all history,
    otherwise the N most recent dates. Players are identified by UCID (stored
    in the entry, or via name_to_ucid), then exact name, then callsign-stripped
    name, and shown with their CURRENT name and rank; unknown players show the
    stored name without rank. min3_latest_day forces at least 3 positions on
    the newest date (used when "P" is combined with other tables).
    Field-size limits are handled by _add_podium_field. None if nothing to show.
    """
    if not history:
        return None

    dates_desc = sorted(history.keys(), reverse=True)
    if days and days > 0:
        dates_desc = dates_desc[:days]

    medals = ["🥇", "🥈", "🥉"]
    blocks: list[list[str]] = []
    name_to_ucid = name_to_ucid or {}
    ucid_to_current = {d.get("ucid"): (n, d) for n, d in players.items() if d.get("ucid")}
    players_by_base = _stripped_index(players)
    for date_idx, date_str in enumerate(dates_desc):
        is_latest_day  = (date_idx == 0)
        effective_top  = max(top, 3) if (is_latest_day and min3_latest_day) else top
        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            ts = int(dt.timestamp())
            date_disp = f"<t:{ts}:d>"
        except ValueError:
            date_disp = date_str
        for event in history[date_str]:
            top_list = event.get("top") or []
            n = min(effective_top, len(top_list))
            if n <= 0:
                continue
            suffix = " (Session End)" if event.get("campaign_restart") else ""

            # Build each position's (marker, line_body) first — only then
            # decide the header format, since an invalid entry (no name)
            # could reduce the ACTUAL count below `n`.
            position_lines: list[tuple[str, str]] = []
            for idx in range(n):
                entry = top_list[idx]
                name, pts = entry.get("name"), entry.get("points", 0)
                if not name:
                    continue
                marker  = medals[idx] if idx < 3 else "🎖️"
                # UCID first (stored since v12.1.1, else via name_to_ucid): survives
                # renames name matching can't ("Viper**" -> "Viper").
                current_name, player_data = None, None
                ucid = entry.get("ucid") or name_to_ucid.get(name)
                if ucid and ucid in ucid_to_current:
                    current_name, player_data = ucid_to_current[ucid]
                if not player_data:
                    player_data = players.get(name)
                    if player_data:
                        current_name = name
                if not player_data:
                    # Last resort: callsign-stripped comparison (history and Foothold_Ranks.lua
                    # may differ by the flight-callsign prefix).
                    p_name = players_by_base.get(strip_callsign(name))
                    if p_name is not None:
                        current_name, player_data = p_name, players[p_name]
                # Current name if the player could be identified; otherwise
                # (excluded, or no longer in Foothold_Ranks.lua) the name
                # stored for that day, without a rank.
                shown   = current_name or name
                display = strip_callsign(shown) if strip_callsign_flag else shown
                short   = _safe_code_span(display)
                if player_data:
                    rank = player_data.get("custom_rank") or get_rank(float(player_data.get("credits", 0)))
                    rank = _fit_rank(f"{display} — ", rank, f" — {int(pts):,} pts")
                    rank_part = f" — **{rank}**"
                else:
                    rank_part = ""
                position_lines.append((marker, f"{short}{rank_part} — {int(pts):,} pts"))

            if not position_lines:
                continue

            if len(position_lines) == 1:
                # Single position for this event — compact one-line form:
                # date, medal, name, rank and points all on the same line,
                # instead of a separate underlined date header above it.
                marker, line_body = position_lines[0]
                block = [f"__{date_disp}__ {marker} {line_body}{suffix}"]
            else:
                block = [f"__{date_disp}__{suffix}"]
                for marker, line_body in position_lines:
                    block.append(f"{marker} {line_body}")
            blocks.append(block)

    if not blocks:
        return None

    return "\n".join("\n".join(b) for b in blocks)


def _chunk_lines(lines: list[str], limit: int) -> list[list[str]]:
    """Group lines into chunks whose newline-joined length stays within
    `limit` (Discord's per-field cap), never splitting a line. A single line
    longer than `limit` gets a chunk of its own."""
    chunks, cur, cur_len = [], [], 0
    for line in lines:
        ll = len(line) + 1
        if cur and cur_len + ll > limit:
            chunks.append(cur)
            cur, cur_len = [], 0
        cur.append(line)
        cur_len += ll
    if cur:
        chunks.append(cur)
    return chunks


def _add_podium_field(embed: discord.Embed, icon: str, podium_text: str) -> None:
    """Add the Podium text as one or more fields (1024-char field limit),
    respecting Discord's 25-fields-per-embed cap: if it doesn't fit, the
    listing is truncated with a "+ N more" note, keeping room for the
    trailing ruler.
    """
    MAX_EMBED_FIELDS    = DISCORD_MAX_FIELDS
    RESERVED_FOR_TRAILER = 2

    title = f"{icon} __Daily Podium__"
    cont_title = f"{icon} __Daily Podium (cont.)__"
    FIELD_LIMIT = 1020
    chunks = ["\n".join(c) for c in _chunk_lines(podium_text.split("\n"), FIELD_LIMIT)]

    available = MAX_EMBED_FIELDS - len(embed.fields) - RESERVED_FOR_TRAILER
    if available <= 0:
        return  # No room left at all this cycle — Podium silently omitted
                # rather than risk pushing the embed over Discord's field cap.
    if len(chunks) > available:
        kept = chunks[:available]
        dropped_lines = sum(c.count("\n") + 1 for c in chunks[available:])
        note = f"*+ {dropped_lines} more*"
        last = kept[-1]
        if len(last) + 1 + len(note) <= FIELD_LIMIT:
            kept[-1] = last + "\n" + note
        else:
            kept[-1] = note
        chunks = kept

    for i, chunk in enumerate(chunks):
        embed.add_field(name=title if i == 0 else cont_title, value=chunk, inline=False)


DISCORD_EMBED_LIMIT = 6000  # Discord hard limit for total embed size


_pilot_cap_warned: set[str] = set()


def _pilot_cap(cfg: dict, role: str) -> int | None:
    """How many pilots one table (role = R, S or D) may list: max_pilots_<role>,
    else max_pilots, else no limit (None). A limit is a whole number of 1 or
    more (0 also means no limit); anything else is ignored with one warning."""
    for key in (f"max_pilots_{role}", "max_pilots"):
        value = cfg.get(key)
        if value is None or value == "":
            continue
        ok = (isinstance(value, int) and not isinstance(value, bool)) or \
             (isinstance(value, str) and value.strip().isdigit())
        if ok and int(value) >= 0:
            return int(value) or None
        if f"{key}={value!r}" not in _pilot_cap_warned:
            _pilot_cap_warned.add(f"{key}={value!r}")
            log.warning(f"Fh_Report: {key} {value!r} is not a whole number of 1 or more — no limit applied.")
        return None
    return None


DISCORD_MAX_FIELDS = 25   # Discord rejects embeds with more fields


def _cap_fields(embed: discord.Embed, max_fields: int) -> None:
    """Drop trailing fields beyond `max_fields`, marking the last kept one —
    a long show_all_pilots listing across several tables can otherwise
    exceed Discord's field cap and get the whole update rejected."""
    if len(embed.fields) <= max_fields:
        return
    while len(embed.fields) > max_fields:
        embed.remove_field(max_fields)
    last = embed.fields[-1]
    note = "\n*…trimmed*"
    value = last.value if len(last.value) + len(note) <= 1024 else last.value[:1024 - len(note)]
    embed.set_field_at(max_fields - 1, name=last.name, value=value + note, inline=last.inline)


def _embed_size(embed: discord.Embed) -> int:
    """Calculate total character count of a Discord embed."""
    total = 0
    if embed.title:
        total += len(embed.title)
    if embed.description:
        total += len(embed.description)
    if embed.footer and embed.footer.text:
        total += len(embed.footer.text)
    for field in embed.fields:
        total += len(field.name or "") + len(field.value or "")
    return total


def _trim_embed(embed: discord.Embed) -> discord.Embed:
    """Trim embed fields to fit within Discord 6000 char limit.
    Removes pilot lines from leaderboard fields first, then zone lines."""
    if _embed_size(embed) <= DISCORD_EMBED_LIMIT:
        return embed

    # Identify and trim pilot fields first (fields with medal emojis)
    for i, field in enumerate(embed.fields):
        if _embed_size(embed) <= DISCORD_EMBED_LIMIT:
            break
        val = field.value or ""
        lines = val.split("\n")
        # Trim from the bottom until it fits
        while len(lines) > 1 and _embed_size(embed) > DISCORD_EMBED_LIMIT:
            lines.pop()
            new_val = "\n".join(lines) + "\n*…trimmed*"
            embed.set_field_at(i, name=field.name, value=new_val, inline=field.inline)

    # If still too large, trim zone fields
    for i, field in enumerate(embed.fields):
        if _embed_size(embed) <= DISCORD_EMBED_LIMIT:
            break
        val = field.value or ""
        if "🔹" in val or "🔺" in val or "◇" in val or "△" in val:
            lines = val.split("\n")
            while len(lines) > 1 and _embed_size(embed) > DISCORD_EMBED_LIMIT:
                lines.pop()
                new_val = "\n".join(lines) + "\n*…trimmed*"
                embed.set_field_at(i, name=field.name, value=new_val, inline=field.inline)

    return embed


def _fmt_compact(n: int) -> str:
    """Format a number compactly: <1000 exact, then k / M with 1 decimal
    (stripped if .0) — keeps long values like fuel lbs from widening lines."""
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        return f"{n / 1000:.1f}".rstrip("0").rstrip(".") + "k"
    return f"{n / 1_000_000:.1f}".rstrip("0").rstrip(".") + "M"


def _build_pilot_card(career: dict, icon: str = "🔸", bnb: int = 0) -> str | None:
    """One-line career card from Foothold_Ranks.lua career stats (fixed/helo
    hours, kills, traps, fuel received, deaths). None if all are zero.
    """
    total_s  = int(career.get(1, 0))
    helo_s   = int(career.get(3, 0))
    fixed_s  = total_s - helo_s
    kills    = int(career.get(10, 0))
    traps    = int(career.get(8, 0))
    refuel_lbs = int(career.get(30, 0))
    deaths   = int(career.get(21, 0))

    def _fmt_time(seconds: int) -> str | None:
        """Format seconds as hours (>=1h) or minutes (<1h). None if zero."""
        if seconds <= 0:
            return None
        hours = seconds // 3600
        if hours >= 1:
            return f"{hours}h"
        minutes = max(1, seconds // 60)  # at least 1m if there's any time
        return f"{minutes}m"

    parts = []
    fixed_str = _fmt_time(fixed_s)
    if fixed_str: parts.append(f"{fixed_str} Fixed")
    helo_str  = _fmt_time(helo_s)
    if helo_str:  parts.append(f"{helo_str} Helo")
    if kills > 0:      parts.append(f"{kills} Kills")
    if traps > 0:      parts.append(f"{traps} Traps")
    if refuel_lbs > 0: parts.append(f"{_fmt_compact(refuel_lbs)} lbs")
    if bnb > 0:        parts.append(f"BoB: {bnb}")   # penultimate, never dropped
    if deaths > 0:     parts.append(f"{deaths} Deaths")

    if not parts:
        return None
    return f"·　{icon} " + " · ".join(parts)


# Non-mission playerStats keys from zoneCommander.lua (stat labels, kill
# categories, Refueling, Zone supply delivery). Any other key counts as a
# mission objective — including map-specific ones like "Destroy enemy Bridge".
_NON_MISSION_STATS = frozenset({
    "Air", "Helo", "Ground Units", "Ship", "SAM", "Structure", "Infantry",
    "Demolition kill", "Deaths", "Captured by enemy", "Zone capture",
    "Zone upgrade", "Zone supply delivery", "Pilot Rescue", "Refueling",
    "Points", "Points spent", "Flight time", "Achievement", "BoB",
})


def _is_mission_stat(key: str) -> bool:
    """True if a playerStats key counts as a mission objective — see
    _NON_MISSION_STATS. Keys mentioning "mission" always count (e.g.
    "Bomb runway (Joint mission)"); a " (troops)" suffix is ignored when
    matching the non-mission list (e.g. "Zone capture (troops)")."""
    if "mission" in key.lower():
        return True
    base = key[:-len(" (troops)")] if key.endswith(" (troops)") else key
    return base not in _NON_MISSION_STATS


def _build_session_card(raw_stats: dict, icon: str = "🔸", bnb: int = 0) -> str | None:
    """One-line session/daily card from raw playerStats keys, in priority
    order: Msn (every mission objective, see _is_mission_stat), Ach, Air
    (Air+Helo), SAM, Ground (Ground Units+Structure+Infantry), Ship, Resc,
    Refuels, Deaths. Capped at 7 entries, dropping the lowest priority first
    but always keeping BoB (blue-on-blue, from DCSSB) and Deaths. 'Flight time' is left out on purpose: Foothold
    only records it for a few transport/helo types, so it would read 0 for
    most pilots. None if everything is zero.
    """
    if not raw_stats and bnb <= 0:
        return None
    raw_stats = raw_stats or {}

    missions = sum(
        int(v) for k, v in raw_stats.items()
        if _is_mission_stat(k) and isinstance(v, (int, float)) and v > 0
    )
    achievement = int(raw_stats.get("Achievement", 0))
    air    = int(raw_stats.get("Air", 0)) + int(raw_stats.get("Helo", 0))
    sam    = int(raw_stats.get("SAM", 0))
    ground = (int(raw_stats.get("Ground Units", 0)) + int(raw_stats.get("Structure", 0))
              + int(raw_stats.get("Infantry", 0)))
    ship    = int(raw_stats.get("Ship", 0))
    rescues = int(raw_stats.get("Pilot Rescue", 0))
    refuels = int(raw_stats.get("Refueling", 0))
    deaths  = int(raw_stats.get("Deaths", 0))

    # (priority_rank, label_text) — lower rank = higher priority, always kept first
    candidates = [
        (0, f"{missions} Msn") if missions > 0 else None,
        (1, f"{achievement} Ach") if achievement > 0 else None,
        (2, f"{air} Air") if air > 0 else None,
        (3, f"{sam} SAM") if sam > 0 else None,
        (4, f"{ground} Ground") if ground > 0 else None,
        (5, f"{ship} Ship") if ship > 0 else None,
        (6, f"{rescues} Resc") if rescues > 0 else None,
        (7, f"{refuels} Refuels") if refuels > 0 else None,
        (8, f"BoB: {bnb}") if bnb > 0 else None,
        (9, f"{deaths} Death" + ("s" if deaths != 1 else "")) if deaths > 0 else None,
    ]
    candidates = [c for c in candidates if c is not None]

    # Cap at 7 fields — drop lowest-priority fields first, but always keep
    # BoB and deaths (ranks 8 and 9)
    if len(candidates) > 7:
        kept   = [c for c in candidates if c[0] >= 8]
        others = sorted((c for c in candidates if c[0] < 8), key=lambda c: c[0])[:7 - len(kept)]
        candidates = sorted(others + kept, key=lambda c: c[0])

    parts = [label for _, label in candidates]

    if not parts:
        return None
    return f"·　{icon} " + " · ".join(parts)


# Display order of individual stat keys in /fh_report player (same grouping
# as _build_session_card, without summing). Missions first, Deaths last.
_STAT_KEY_ORDER = [
    "Achievement", "Air", "Helo", "SAM",
    "Infantry", "Ground Units", "Structure",
    "Ship", "Pilot Rescue", "Refueling",
]


def _display_stat_label(key: str) -> str:
    """Display label for a raw playerStats key. 'Flight time' is Foothold's
    transport-only landing counter, not total hours, hence the relabel.
    """
    if key == "Flight time":
        return "Transport Flight Time"
    return key


def _display_stat_value(key: str, value: float) -> str:
    """Unit-aware value: 'Flight time' is minutes (shown as Xh Ym),
    'Refueling' is a count of refuel events; everything else is numeric.
    """
    if key == "Flight time":
        total_min = int(value)
        h, m = divmod(total_min, 60)
        return f"{h}h {m}m" if h > 0 else f"{m}m"
    if key == "Refueling":
        n = int(value)
        return f"{n} event" if n == 1 else f"{n} events"
    return _fmt_num(value)


def _order_stat_items(stats: dict) -> list[tuple[str, float]]:
    """Sort a raw playerStats dict into the fixed display order used by the
    full-detail stats sections: Missions (every mission objective, incl.
    map-specific ones — see _is_mission_stat), Achievement, Air, Helo, SAM,
    Infantry, Ground Units, Structure, Ship, Pilot Rescue, Refueling, any
    other known keys (alphabetical), BoB, then Deaths always last."""
    def rank(key: str) -> tuple[int, str]:
        if key == "Deaths":
            return (99, key)
        if key == "BoB":
            return (98, "\uffff")   # penultimate: after every other category, right before Deaths
        if key in _STAT_KEY_ORDER:
            return (_STAT_KEY_ORDER.index(key) + 1, key)
        if _is_mission_stat(key):
            return (0, key)
        return (98, key)  # unrecognized — after known categories, before Deaths

    return sorted(stats.items(), key=lambda kv: rank(kv[0]))


def _single_channel_id(value):
    """channel_id as a scalar, accepting a one-item YAML list too (the first
    item of a longer list).
    """
    if isinstance(value, list):
        return value[0] if value else None
    return value


def _add_table_field(embed: discord.Embed, name: str, table_text: str, limit: int = 1024) -> None:
    """Add a ```code block``` table as one or more fields, splitting on line
    boundaries to respect Discord's 1024-char field limit. Continuation
    fields get a zero-width name.
    """
    if len(table_text) <= limit:
        embed.add_field(name=name, value=table_text, inline=False)
        return
    inner = table_text.strip()
    if inner.startswith("```"):
        inner = inner[3:]
    if inner.endswith("```"):
        inner = inner[:-3]
    fence_overhead = len("```\n\n```")
    chunks = ["```\n" + "\n".join(c) + "\n```"
              for c in _chunk_lines(inner.strip("\n").split("\n"), limit - fence_overhead)]
    embed.add_field(name=name, value=chunks[0], inline=False)
    for chunk in chunks[1:]:
        embed.add_field(name="\u200b", value=chunk, inline=False)


def _build_combined_stats_table(session_stats: dict, daily_stats: dict | None) -> str:
    """Monospace table with Session and Daily values side by side for every
    category present in either; '-' marks a missing value (never '0').
    Ordering uses _order_stat_items, which recognises map-specific missions.
    """
    daily_stats = daily_stats or {}
    all_keys = set(session_stats.keys()) | set(daily_stats.keys())
    ordered_keys = [k for k, _ in _order_stat_items({k: 0 for k in all_keys})]

    name_width = max([len(_display_stat_label(k)) for k in ordered_keys] + [len("Category")])
    session_vals = [_display_stat_value(k, session_stats[k]) for k in ordered_keys if k in session_stats]
    daily_vals   = [_display_stat_value(k, daily_stats[k]) for k in ordered_keys if k in daily_stats]
    session_width = max([len(v) for v in session_vals] + [len("Session")])
    daily_width   = max([len(v) for v in daily_vals] + [len("Daily")])

    header = f"{'Category':<{name_width}}  {'Session':>{session_width}}  {'Daily':>{daily_width}}"
    lines = [header, "-" * len(header)]
    for k in ordered_keys:
        label = _display_stat_label(k)
        s_val = _display_stat_value(k, session_stats[k]) if k in session_stats else "-"
        d_val = _display_stat_value(k, daily_stats[k]) if k in daily_stats else "-"
        lines.append(f"{label:<{name_width}}  {s_val:>{session_width}}  {d_val:>{daily_width}}")
    return "```\n" + "\n".join(lines) + "\n```"


_SEPARATOR = "▬" * 32


def _build_player_report_embed(player_name: str, data: dict, ucid: str | None,
                               last_seen, session_points: float,
                               daily_points: float, session_stats: dict,
                               mission_status: str,
                               daily_stats: dict | None = None,
                               bnb: dict | None = None,
                               penalties: dict | None = None) -> discord.Embed:
    """Build a read-only, info-only player embed for /fh_report player.
    No buttons, no editing — mirrors Fh_Control's player embed sections
    (UCID, points, session stats, career stats, mission) in display-only form.
    """
    credits = float(data.get("credits", 0))
    embed = discord.Embed(
        title=f"👤 Player — {player_name}",
        color=0x3498DB, timestamp=datetime.now(timezone.utc)
    )

    # ── UCID ────────────────────────────────────────────────────────────
    if ucid:
        embed.add_field(name="\u200b", value=f"🔑 UCID: {ucid}", inline=False)

    # ── Last seen ───────────────────────────────────────────────────────
    embed.add_field(name="\u200b", value=_SEPARATOR, inline=False)
    if last_seen is not None:
        ts = int(calendar.timegm(last_seen.timetuple()))
        activity_line = f"- **Last seen:** <t:{ts}:F> (<t:{ts}:R>)"
    else:
        activity_line = "- **Last seen:** —"
    embed.add_field(name="🕒 __Activity__", value=activity_line, inline=False)

    # ── Session + Daily stats in one aligned table (Points shown in the title) ─
    embed.add_field(name="\u200b", value=_SEPARATOR, inline=False)
    # BoB (blue-on-blue, from DCSSB) is one more row of the table, with its session
    # and daily counts like any other category; absent when zero.
    session_stats = dict(session_stats or {})
    if bnb and bnb.get("session_known", True) and bnb.get("session"):
        session_stats["BoB"] = bnb["session"]
    if bnb and daily_stats is not None and bnb.get("day"):
        daily_stats = {**daily_stats, "BoB": bnb["day"]}
    other_stats = {k: v for k, v in session_stats.items() if k != "Points"} if session_stats else {}
    daily_filtered = {k: v for k, v in daily_stats.items() if k != "Points" and v} if daily_stats else {}
    title_parts = [f"📊 __Session Stats__ (S: {_fmt_num(session_points)})"]
    if daily_stats is not None:
        title_parts.append(f"📅 __Daily__ (D: {_fmt_num(daily_points)})")
    combined_title = "                                            ".join(title_parts)
    if other_stats or daily_filtered:
        _add_table_field(embed, combined_title,
                         _build_combined_stats_table(other_stats, daily_filtered))
    else:
        embed.add_field(name=combined_title,
                        value="_No stats yet — will appear after first flight._", inline=False)

    # ── Blue-on-blue (from DCSServerBot's Mission Statistics) ───────────
    # Only what the stats are about: this session and today. The all-time total
    # is left out on purpose (it is on the rank card), and nothing is shown when
    # neither has an incident.
    if bnb and (bnb.get("session") or bnb.get("day") or bnb.get("recent")):
        session_txt = f"**Session:** {bnb['session']}" if bnb.get("session_known", True) else "**Session:** n/a"
        head = [f"- {session_txt} · **Today:** {bnb['day']}"]
        events = []
        for when, kind, target_id, victim, target_type in bnb.get("recent") or []:
            ts = int(calendar.timegm(when.timetuple()))
            if victim:
                who = f" → {_safe_code_span(victim)}"
            elif target_id:
                who = " → unknown player"
            else:   # no player UCID on the victim: an AI unit
                who = f" → AI unit ({target_type})" if target_type else " → AI unit"
            events.append(f"· <t:{ts}:d> {BNB_LABELS.get(kind, kind)}{who}")
        # Fit Discord's 1024-character field value: drop the oldest lines first,
        # keeping room for the note that says the list is partial.
        def _render(n: int) -> str:
            partial = n < len(events) or bnb.get("more")
            note = [f"_Showing only the latest {n}._"] if partial else []
            return "\n".join(head + events[:n] + note)
        shown = len(events)
        while shown > 0 and len(_render(shown)) > 1024:
            shown -= 1
        embed.add_field(name="\u200b", value=_SEPARATOR, inline=False)
        embed.add_field(name="⚠️ __Blue-on-Blue (BoB)__", value=_render(shown)[:1024], inline=False)

    # ── Penalties in force (Punishment plugin, after decay) ─────────────
    if penalties and penalties.get("total", 0) >= 1:
        badge = get_punishment_badge(penalties["total"])
        parts = [f"{PU_LABELS.get(ev, ev.replace('_', ' ').title())} ×{n} ({pts:.1f} p.p.)"
                 for ev, n, pts in penalties.get("rows") or []]
        lines = [badge] if badge else []
        if parts:
            lines.append("·　" + " · ".join(parts))
        lines.append("_Points fade over time (decay)._")
        embed.add_field(name="\u200b", value=_SEPARATOR, inline=False)
        embed.add_field(name="⚖️ __Penalties in force__", value="\n".join(lines)[:1024], inline=False)

    # ── Career Stats (Foothold v4.5, from Foothold_Ranks.lua) ──────────
    # Rank Points and rank name shown in the section title itself.
    career = data.get("career") or {}
    career_lines = []
    flight_s = career.get(CAREER_FLIGHT_SECONDS, 0)
    helo_s   = career.get(CAREER_HELO_SECONDS, 0)
    fixed_s  = max(0.0, flight_s - helo_s)
    if fixed_s > 0:
        career_lines.append(f"- **Flight Hours (fixed):** {_fmt_career_time(fixed_s)}")
    if helo_s > 0:
        career_lines.append(f"- **Flight Hours (helo):** {_fmt_career_time(helo_s)}")
    if career.get(CAREER_KILLS, 0) > 0:
        career_lines.append(f"- **Kills:** {int(career[CAREER_KILLS])}")
    if bnb and bnb.get("total"):    # the same all-time number as the rank card, with its detail
        career_lines.append(f"- **Blue-on-Blue (BoB):** {bnb['total']} "
                            f"({bnb['destroyed']} destroyed · {bnb['damaged']} damaged)")
    if career.get(CAREER_TRAPS, 0) > 0:
        career_lines.append(f"- **Carrier Traps:** {int(career[CAREER_TRAPS])}")
    if career.get(CAREER_FUEL_LBS, 0) > 0:
        career_lines.append(f"- **Fuel Received:** {_fmt_compact(int(career[CAREER_FUEL_LBS]))} lbs")
    if career.get(CAREER_DEATHS, 0) > 0:
        career_lines.append(f"- **Pilot Deaths:** {int(career[CAREER_DEATHS])}")
    if career_lines:
        embed.add_field(name="\u200b", value=_SEPARATOR, inline=False)
        embed.add_field(
            name=f"🏆 __Career Stats__ (R: {_fmt_num(credits)} — {get_rank(credits)})",
            value="\n".join(career_lines), inline=False)

    # ── Mission ─────────────────────────────────────────────────────────
    embed.add_field(name="\u200b", value=_SEPARATOR, inline=False)
    embed.add_field(name="🖥️ __Mission__", value=mission_status, inline=False)

    embed.set_footer(text=f"{_version_text()} · Read-only player report")
    return embed


# ── report_layout engine ──────────────────────────────────────────────────────
# report_layout is a string of D/P/S/R letters naming the tables to render,
# in order. points_detail_D/S/R list exactly which values each table shows
# (a role without its key shows only its own value). Legacy points_order /
# compact_points are converted once in the YAML by migrate_config.py.

_ROLE_TITLES = {
    "D": "📅 __Daily Leaderboard · by Today's Points__",
    "S": "📊 __Session Leaderboard · by Current Session__",
    "R": "🏆 __Pilot Leaderboard · by Rank__",
}
_ROLE_ICONS = {"D": "📅", "S": "📊", "R": "🏆"}
_TABLE_MEDALS = ["🥇", "🥈", "🥉"] + ["🎖️"] * 50


def _emit_table_field(embed: discord.Embed, title: str, icon: str,
                      lines: list[str], hidden: int, show_all_pilots: bool) -> None:
    """Add one leaderboard table to the embed. show_all_pilots=True splits it
    into several fields with '__Pilots #a-b__' headers so nobody is dropped;
    otherwise it's cut at the field limit with a '+ N more pilots' note.
    """
    if not lines:
        return
    FIELD_LIMIT = 1020
    if show_all_pilots:
        all_lines = list(lines)
        if hidden > 0:
            all_lines.append(f"*+ {hidden} more pilots*")
        is_more_note = lambda line: line.startswith("*+ ") and line.endswith(" more pilots*")
        chunks = [(c, sum(1 for line in c if not is_more_note(line)))
                  for c in _chunk_lines(all_lines, FIELD_LIMIT)]
        position = 1
        for i, (chunk_lines, chunk_count) in enumerate(chunks):
            chunk = "\n".join(chunk_lines)
            if i == 0:
                name = f"\n{title}"
            elif chunk_count > 0:
                name = f"{icon} __Pilots #{position}–{position + chunk_count - 1}__"
            else:
                name = "\u200b"
            embed.add_field(
                name=name,
                value=(f"\n{chunk}" if i == 0 else chunk)[:1024],
                inline=False
            )
            position += chunk_count
    else:
        visible_lines, used = [], 0
        for i, line in enumerate(lines):
            line_len = len(line) + 1
            lines_after = len(lines) - i - 1
            total_hidden = hidden + lines_after
            more_label = f"\n*+ {total_hidden} more pilots*" if total_hidden > 0 else ""
            if used + line_len + len(more_label) > FIELD_LIMIT:
                break
            visible_lines.append(line)
            used += line_len
        lines_not_shown = len(lines) - len(visible_lines)
        total_hidden_final = hidden + lines_not_shown
        value = "\n" + "\n".join(visible_lines)
        if total_hidden_final > 0:
            value += f"\n*+ {total_hidden_final} more pilots*"
        embed.add_field(name=f"\n{title}", value=value[:1024], inline=False)


def _render_layout_tables(
    embed: discord.Embed, cfg: dict, report_layout: str, points_detail: dict,
    players: dict, dp: dict, has_daily: bool, punishment_points: dict | None,
    daily_history: dict | None, name_to_ucid: dict | None = None,
) -> None:
    """Render every table/Podium named in report_layout, in order, onto
    embed (table options read from `cfg`). `points_detail` is a dict
    {"D": "...", "S": "...", "R": "..."} — a role missing from it shows only
    its own value. See the module-level comment above for the grammar."""
    pilot_caps          = {role: _pilot_cap(cfg, role) for role in ("R", "S", "D")}
    show_punishment     = _bool_cfg(cfg.get("show_punishment"))
    show_all_pilots     = _bool_cfg(cfg.get("show_all_pilots"))
    strip_callsign_flag = _bool_cfg(cfg.get("strip_callsign"))
    show_pilot_card     = _bool_cfg(cfg.get("show_pilot_card"))
    pilot_card_icon     = str(cfg.get("pilot_card_icon") or "🔸")
    show_session_card   = _bool_cfg(cfg.get("show_session_card"))
    session_card_icon   = str(cfg.get("session_card_icon") or "🔸")
    show_daily_card     = _bool_cfg(cfg.get("show_daily_card"))
    daily_card_icon     = str(cfg.get("daily_card_icon") or "🔸")
    podium_days         = int(cfg.get("podium_days") if cfg.get("podium_days") is not None else 7)
    podium_top          = max(1, min(50, int(cfg.get("podium_top") or 1)))
    podium_combined_days = int(cfg.get("podium_combined_days") if cfg.get("podium_combined_days") is not None else 7)
    podium_combined_top  = max(1, min(50, int(cfg.get("podium_combined_top") or 1)))
    podium_combined_min3_latest_day = _bool_cfg(cfg.get("podium_combined_min3_latest_day"))
    layout = (report_layout or "R").strip().upper()
    if layout == "NONE":
        return  # campaign progress and bases only — no leaderboard tables
    points_detail = points_detail or {}
    roles_in_layout = [c for c in layout if c in ("R", "S", "D")]

    # Punishment badge goes on the first table whose role has the highest
    # priority (R > S > D) among roles actually present — same R>S>D
    # priority the legacy multi-table modes always used.
    _priority = {"R": 0, "S": 1, "D": 2}
    badge_role = min(roles_in_layout, key=lambda r: _priority[r]) if roles_in_layout else None
    badge_role_done = False

    pp = punishment_points or {}

    for letter in layout:
        if letter == "P":
            if layout == "P":
                p_lines = _build_podium_table(
                    daily_history or {}, players, days=podium_days, top=podium_top,
                    strip_callsign_flag=strip_callsign_flag, name_to_ucid=name_to_ucid
                )
            else:
                p_lines = _build_podium_table(
                    daily_history or {}, players, days=podium_combined_days, top=podium_combined_top,
                    strip_callsign_flag=strip_callsign_flag, min3_latest_day=podium_combined_min3_latest_day,
                    name_to_ucid=name_to_ucid
                )
            if p_lines:
                _add_podium_field(embed, "👑", p_lines)
            continue

        if letter not in ("R", "S", "D"):
            continue  # unrecognized letter — ignore rather than error
        role = letter

        if role == "D" and not has_daily:
            continue  # no daily data recorded at all this cycle

        if role == "R":
            items = [(n, d) for n, d in players.items() if d.get("credits", 0) > 0]
            items.sort(key=lambda x: x[1]["credits"], reverse=True)
        elif role == "S":
            items = [(n, d) for n, d in players.items() if d.get("session_points", 0) > 0]
            items.sort(key=lambda x: x[1].get("session_points", 0), reverse=True)
        else:  # D
            items = [(n, d) for n, d in players.items() if dp.get(n, 0) > 0 or d.get("daily_stats")]
            items.sort(key=lambda x: dp.get(x[0], 0), reverse=True)

        if not items:
            continue

        total_items = len(items)
        limit = pilot_caps[role]
        if limit:
            items = items[:limit]
        hidden = total_items - len(items)

        lines = []
        for i, (name, data) in enumerate(items):
            credits = int(data["credits"])
            rank    = data.get("custom_rank") or get_rank(credits)
            display = strip_callsign(name) if strip_callsign_flag else name
            short_src = display if len(display) <= 22 else display[:20] + '..'
            short   = _safe_code_span(short_src)
            medal   = data.get("custom_medal") or (_TABLE_MEDALS[i] if i < len(_TABLE_MEDALS) else "•")

            s_pts        = data.get("session_points", 0)
            d_pts        = dp.get(name, 0)
            hide_credits = data.get("hide_credits", False)
            hide_session = data.get("hide_session", False)
            show_d       = has_daily and d_pts > 0

            metrics = {
                "R": (f"R: {credits:,}" if not hide_credits else None),
                "S": (f"S: {s_pts:,}"   if not hide_session and s_pts else None),
                "D": (f"D: {d_pts:,}"   if show_d else None),
            }
            order = [c for c in points_detail.get(role, role) if c in ("R", "S", "D")]
            parts = [metrics[c] for c in order if metrics.get(c)]
            pts_str = f"({'  ·  '.join(parts)})" if parts else ""

            rank  = _fit_rank(f"{short_src} — ", rank, f" {pts_str}")
            block = [f"{medal} {short} — **{rank}** {pts_str}".rstrip()]

            if show_pilot_card and role == "R":
                card = _build_pilot_card(data.get("career") or {}, icon=pilot_card_icon,
                                         bnb=(data.get("bnb") or {}).get("total", 0))
                if card:
                    block.append(card)
            if show_session_card and role == "S":
                s_card = _build_session_card(data.get("session_stats") or {}, icon=session_card_icon,
                                             bnb=(data.get("bnb") or {}).get("session", 0))
                if s_card:
                    block.append(s_card)
            if show_daily_card and role == "D":
                d_card = _build_session_card(data.get("daily_stats") or {}, icon=daily_card_icon,
                                             bnb=(data.get("bnb") or {}).get("day", 0))
                if d_card:
                    block.append(d_card)

            if show_punishment and role == badge_role and not badge_role_done:
                ucid = data.get("ucid")
                if "hook_punishment" in data:
                    pts = data["hook_punishment"]
                elif pp and ucid:
                    pts = pp.get(ucid, 0)
                else:
                    pts = 0
                badge = get_punishment_badge(
                    pts, "", data.get("punishment_icon", ""),
                    data.get("punishment_label", ""), data.get("punishment_pre_icon", "")
                )
                if badge:
                    block.append(badge)

            lines.append("\n".join(block))

        if role == badge_role:
            badge_role_done = True  # only the first table of this role gets badges

        _emit_table_field(embed, _ROLE_TITLES[role], _ROLE_ICONS[role], lines, hidden, show_all_pilots)


def _stripped_index(d: dict) -> dict:
    """{strip_callsign(key): first key with that stripped form} — lets
    callsign-insensitive lookups run in O(1) instead of rescanning `d`."""
    idx: dict = {}
    for k in d:
        idx.setdefault(strip_callsign(k), k)
    return idx


def _zone_lines(side_zones: list, full: str, empty: str, max_zones: int | None,
                zone_name_length: int, slot_status: bool, waypoint_map: dict | None,
                wp_desc: bool, use_true_max: bool) -> list[str]:
    """One side's zone list: active zones first (by waypoint number when a
    waypoint map is given — BLUE descending, RED ascending — else by
    level+slots), suspended zones last. BLUE draws against its true max slot
    count (unlocked-but-unbuilt slots show as empty symbols)."""
    by_level = lambda z: (z["level"], z.get("active_slots", 0))
    active    = [z for z in side_zones if not z.get("suspended")]
    suspended = sorted((z for z in side_zones if z.get("suspended")), key=lambda z: z["level"], reverse=True)
    if waypoint_map:
        with_wp = sorted((z for z in active if z["name"] in waypoint_map),
                         key=lambda z: waypoint_map[z["name"]], reverse=wp_desc)
        active  = with_wp + sorted((z for z in active if z["name"] not in waypoint_map),
                                   key=by_level, reverse=True)
    else:
        active = sorted(active, key=by_level, reverse=True)
    ordered = active + suspended
    shown   = ordered[:max_zones] if max_zones else ordered

    lines = []
    for z in shown:
        if slot_status and not z.get("suspended"):
            total = (z.get("true_max") or z["level"]) if use_true_max else z["level"]
            n_total, n_active = _slot_display_counts(total, z.get("active_slots", z["level"]))
            stars = full * n_active + empty * (n_total - n_active)
        else:
            stars = full * min(z["level"], 5)
        lines.append(f"`{z['name'][:zone_name_length]}` {stars}")
    if max_zones and len(ordered) > max_zones:
        lines.append(f"*+ {len(ordered) - max_zones} more bases*")
    return lines


def build_embed(zones: dict, players: dict, cfg: dict, *,
                report_layout: str = "R",
                points_detail: dict | None = None,
                campaign_stats: dict | None = None,
                session_stats_raw: dict | None = None,
                daily_points: dict | None = None,
                daily_stats_raw: dict | None = None,
                punishment_points: dict | None = None,
                daily_history: dict | None = None,
                waypoint_map: dict | None = None,
                name_to_ucid: dict | None = None,
                map_name: str | None = None,
                bnb: dict | None = None) -> discord.Embed:
    """Build the Discord embed from parsed Foothold data. `bnb` is
    {ucid: {"total", "day", "session"}} blue-on-blue counts from DCSSB. Display options
    are read from `cfg` (merged DEFAULT + instance block of fh_report.yaml)."""
    campaign_name       = cfg.get("campaign_name", "Foothold Campaign")
    max_zones           = cfg.get("max_zones") or None
    bar_length          = int(cfg.get("bar_length") or 40)
    bar_style_emoji     = _bool_cfg(cfg.get("bar_style_emoji"))
    slot_status         = _bool_cfg(cfg.get("slot_status"))
    zone_name_length    = max(8, min(24, int(cfg.get("zone_name_length") or 16)))
    sort_zones_by_waypoint = _bool_cfg(cfg.get("sort_zones_by_waypoint"))
    _now_ts    = int(datetime.now(timezone.utc).timestamp())
    timestamp  = f"<t:{_now_ts}:f>"
    blue_count  = len(zones["blue"])
    red_count   = len(zones["red"])
    neutral_count = zones.get("neutral", 0)
    total         = blue_count + red_count + neutral_count
    active_total  = blue_count + red_count

    pct_blue     = round(blue_count / active_total * 100) if active_total > 0 else 50
    pct_red      = 100 - pct_blue

    if bar_style_emoji:
        # Emoji mode — each emoji is double-width so divide bar_length by 2.
        # Recommended for mobile Discord compatibility.
        effective_length = max(1, bar_length // 2)
        blue_bars    = round((blue_count / total) * effective_length) if total > 0 else effective_length // 2
        neutral_bars = round((neutral_count / total) * effective_length) if total > 0 else 0
        red_bars     = effective_length - blue_bars - neutral_bars
        bar          = "🟦" * blue_bars + "⬜" * neutral_bars + "🟥" * red_bars
        progress     = f"```\n{pct_blue}% {bar} {pct_red}%\n```"
    else:
        # ANSI mode (default) — single-width █ chars with color codes.
        # Works on Discord Desktop and Browser. Not supported on mobile.
        blue_bars    = round((blue_count / total) * bar_length) if total > 0 else bar_length // 2
        neutral_bars = round((neutral_count / total) * bar_length) if total > 0 else 0
        red_bars     = bar_length - blue_bars - neutral_bars
        ESC          = "\u001b"
        bar_ansi     = (
            f"{ESC}[34m" + "█" * blue_bars +
            f"{ESC}[37m" + "█" * neutral_bars +
            f"{ESC}[31m" + "█" * red_bars +
            f"{ESC}[0m"
        )
        progress     = f"```ansi\n{pct_blue}% {bar_ansi} {pct_red}%\n```"

    blue_text = "\n".join(_zone_lines(zones["blue"], "🔹", "◇", max_zones, zone_name_length, slot_status,
                            waypoint_map if sort_zones_by_waypoint else None, wp_desc=True,
                            use_true_max=True) + ["."])
    red_text  = "\n".join(_zone_lines(zones["red"], "🔺", "△", max_zones, zone_name_length, slot_status,
                            waypoint_map if sort_zones_by_waypoint else None, wp_desc=False,
                            use_true_max=False)) or "—"

    # Embed + zone fields are created here (moved up from just before the
    # tables section) so the report_layout engine below can add its own
    # fields onto the same embed object.
    embed = discord.Embed(
        title=_embed_title(campaign_name, map_name if _show_map(cfg) else None),
        description=f"**Front Status — {timestamp}**\n\n{progress}",
        color=0x3498DB
    )
    embed.add_field(name=f"🔵 BLUE Zones ({blue_count})", value=blue_text[:1024], inline=True)
    embed.add_field(name=f"🔴 RED Zones ({red_count})", value=red_text[:1024], inline=True)

    # Pilot leaderboard — apply session stats and ordering
    cs   = campaign_stats or {}
    srs  = session_stats_raw or {}
    drs  = daily_stats_raw or {}

    # Attach session points / raw session stats / raw daily stats to each
    # player (hook-provided values take priority). Exact name first, then
    # the first callsign-stripped match.
    cs_idx, srs_idx, drs_idx = _stripped_index(cs), _stripped_index(srs), _stripped_index(drs)
    for name, data in players.items():
        base = strip_callsign(name)
        if "session_points" not in data:
            s_pts = cs.get(name, 0)
            if s_pts == 0 and base in cs_idx:
                s_pts = cs[cs_idx[base]]
            data["session_points"] = s_pts
        if "session_stats" not in data:
            raw = srs.get(name)
            if raw is None and base in srs_idx:
                raw = srs[srs_idx[base]]
            data["session_stats"] = raw or {}
        if bnb and "bnb" not in data and data.get("ucid") in bnb:
            data["bnb"] = bnb[data["ucid"]]
        if "daily_stats" not in data:
            draw = drs.get(name)
            if draw is None and base in drs_idx:
                draw = drs[drs_idx[base]]
            data["daily_stats"] = draw or {}

    dp          = daily_points or {}  # {name: daily_pts}
    has_daily   = bool(dp) or any(drs.values())

    _render_layout_tables(
        embed, cfg, report_layout, points_detail, players, dp, has_daily,
        punishment_points, daily_history, name_to_ucid,
    )

    return _finish_embed(embed, cfg)


def _finish_embed(embed: discord.Embed, cfg: dict) -> discord.Embed:
    """Closing ruler, footer and timestamp, then Discord's size limits."""
    campaign_name = cfg.get("campaign_name", "Foothold Campaign")
    # Footer reminder of /fh_report player — on unless explicitly disabled.
    player_cmd_hint = None
    raw_hint_flag   = cfg.get("show_player_cmd_hint")
    if raw_hint_flag is None or _bool_cfg(raw_hint_flag):
        player_cmd_hint = str(cfg.get("player_cmd_hint_text")
                              or "Type /fh_report player to see your own stats.")

    _cap_fields(embed, DISCORD_MAX_FIELDS - 1)   # keep room for the ruler
    # Full-width separator — placed at the bottom to fix embed width
    # without interrupting the visual flow of the content.
    try:
        _ruler_name = utils.print_ruler(ruler_length=34)
    except Exception:
        _ruler_name = "─" * 34
    embed.add_field(name="\u200b", value=_ruler_name, inline=False)
    footer_lines = [_version_text()]
    if player_cmd_hint:
        footer_lines.append(player_cmd_hint)
    footer_lines.append(f"{campaign_name} • Updated automatically")
    embed.set_footer(text="\n".join(footer_lines))
    embed.timestamp = datetime.now(timezone.utc)
    return _trim_embed(embed)


def _version_text() -> str:
    """How the version is written everywhere it shows (embed footers, log)."""
    return f"Fh_Report Ver. {FH_REPORT_RELEASE}"


def _show_map(cfg: dict) -> bool:
    """show_map: on unless explicitly disabled (also when the key is absent)."""
    raw = cfg.get("show_map")
    return True if raw is None else _bool_cfg(raw)


def _show_bob(cfg: dict) -> bool:
    """show_bob: on unless explicitly disabled (also when the key is absent)."""
    raw = cfg.get("show_bob")
    return True if raw is None else _bool_cfg(raw)


def _embed_title(campaign_name: str, map_name: str | None) -> str:
    """Report title; when the map is known it's a second line of the title
    itself, so it shares the title's font. The first line is what identifies
    this instance's message in the channel (see _publish_embed)."""
    title = f"📡  {campaign_name}"
    if map_name:
        title += f"\n🗺️  {map_name}"
    return title[:256]          # Discord's title limit


def build_not_started_embed(players: dict, cfg: dict, report_layout: str, points_detail: dict,
                            punishment_points: dict | None = None,
                            map_name: str | None = None) -> discord.Embed:
    """Embed for an instance whose Foothold mission hasn't created its save
    file yet (new server, or campaign not started). Same title as the real
    report, so that message simply becomes the report once the save appears.
    Shows the rank table when Foothold_Ranks.lua exists and the layout has R."""
    campaign_name = cfg.get("campaign_name", "Foothold Campaign")
    timestamp = f"<t:{int(datetime.now(timezone.utc).timestamp())}:f>"
    embed = discord.Embed(
        title=_embed_title(campaign_name, map_name if _show_map(cfg) else None),
        description=(f"**Front Status — {timestamp}**\n\n"
                     "⏸️ **Campaign not started yet** or this server has no Foothold mission loaded."),
        color=0x95A5A6
    )
    if players and "R" in (report_layout or "").upper():
        _render_layout_tables(embed, cfg, "R", points_detail, players, {}, False,
                              punishment_points, None)
    return _finish_embed(embed, cfg)


# ── Per-player identity (UCID-first) ──────────────────────────────────────────
# Daily/session figures are keyed by player ID: the UCID (from the save, then
# Foothold_Ranks.lua, then fhr_daily_snapshot.json history), else the name for
# old UCID-less saves. Names are for display only.
_UCID_RE = re.compile(r"[0-9a-f]{32}")


def _is_ucid(key) -> bool:
    return isinstance(key, str) and bool(_UCID_RE.fullmatch(key))


def _player_id(name: str, name_to_ucid: dict) -> str:
    return name_to_ucid.get(name) or name


def _group_by_player_id(campaign_stats: dict, session_stats_raw: dict,
                        name_to_ucid: dict) -> tuple[dict, dict, dict]:
    """Regroup name-keyed points and session stats by player ID (UCID, else
    name). Old name-keyed saves start a new entry on every rename, so all
    entries of one UCID are summed. Returns (campaign_by_id, session_by_id,
    names_by_id), names_by_id holding the highest-scoring name per ID.
    """
    campaign_by_id: dict = {}
    best_name: dict = {}
    for name, pts in campaign_stats.items():
        pid = _player_id(name, name_to_ucid)
        campaign_by_id[pid] = campaign_by_id.get(pid, 0) + pts
        if pid not in best_name or pts > campaign_stats.get(best_name[pid], 0):
            best_name[pid] = name
    session_by_id: dict = {}
    for name, stats in session_stats_raw.items():
        pid = _player_id(name, name_to_ucid)
        acc = session_by_id.setdefault(pid, {})
        for key, val in stats.items():
            acc[key] = acc.get(key, 0) + val
        best_name.setdefault(pid, name)
    return campaign_by_id, session_by_id, best_name


_MAX_ALIASES = 5  # past names kept per player (names active in the current save are always kept)


def _pack_daily_snapshot(d: dict, live_names: set | None = None) -> dict:
    """Internal snapshot -> on-disk format_version 2:
        players: { id: { name, aliases?, baseline?, today?, carry? } }
    with baseline/today/carry = {points?, stats?}; empty parts omitted. Past
    names are kept as aliases (at most _MAX_ALIASES, plus any still active).
    """
    players: dict = {}

    def blk(pid):
        return players.setdefault(pid, {})

    for pid, name in (d.get("names") or {}).items():
        blk(pid)["name"] = name
    for part, pts_key, st_key in (("baseline", "snapshot", "stats_snapshot"),
                                  ("today", "last_daily", "last_daily_stats"),
                                  ("carry", "carry_over", "stats_carry_over")):
        for pid, v in (d.get(pts_key) or {}).items():
            blk(pid).setdefault(part, {})["points"] = v
        for pid, st in (d.get(st_key) or {}).items():
            if st:
                blk(pid).setdefault(part, {})["stats"] = st
    for name, ucid in (d.get("name_to_ucid") or {}).items():
        if not ucid:
            continue
        b = blk(ucid)
        if b.get("name") == name:
            continue
        b.setdefault("aliases", [])
        if name not in b["aliases"]:
            b["aliases"].append(name)
    live_names = live_names or set()
    for b in players.values():                    # every block carries a name
        if "name" not in b and b.get("aliases"):
            b["name"] = b["aliases"].pop()
        if b.get("aliases"):
            # Cap aliases, but never drop one still active in the save: in UCID-less
            # saves it's what ties that entry to the player.
            live = [a for a in b["aliases"] if a in live_names]
            past = [a for a in b["aliases"] if a not in live_names]
            room = max(0, _MAX_ALIASES - len(live))
            b["aliases"] = (past[-room:] if room else []) + live
        if "aliases" in b and not b["aliases"]:
            del b["aliases"]
    ordered = {pid: {k: b[k] for k in ("name", "aliases", "baseline", "today", "carry") if k in b}
               for pid, b in players.items()}
    return {
        "format_version":       2,
        "date":                 d.get("date", ""),
        "persistence_filename": d.get("persistence_filename", ""),
        "players":              ordered,
        "known_stat_keys":      d.get("known_stat_keys", []),
    }


def _unpack_daily_snapshot(raw: dict) -> dict:
    """On-disk per-player format -> internal section-per-field dict (the shape the
    daily computation works on). Older formats (pre-12.2.0 name-keyed, or
    the short-lived per-section UCID format) are returned unchanged — they
    already have that shape, and _normalize_snapshot_ids re-keys them."""
    # Detected by structure ("players" key), not by format_version.
    if "players" not in (raw or {}):
        return raw or {}
    out = {
        "date": raw.get("date", ""), "persistence_filename": raw.get("persistence_filename", ""),
        "known_stat_keys": raw.get("known_stat_keys", []),
        "snapshot": {}, "stats_snapshot": {}, "last_daily": {}, "last_daily_stats": {},
        "carry_over": {}, "stats_carry_over": {}, "names": {}, "name_to_ucid": {},
    }
    for pid, b in (raw.get("players") or {}).items():
        if "name" in b:
            out["names"][pid] = b["name"]
            if _is_ucid(pid):
                out["name_to_ucid"][b["name"]] = pid
        for alias in b.get("aliases") or []:
            out["name_to_ucid"][alias] = pid
        for part, pts_key, st_key in (("baseline", "snapshot", "stats_snapshot"),
                                      ("today", "last_daily", "last_daily_stats"),
                                      ("carry", "carry_over", "stats_carry_over")):
            p = b.get(part) or {}
            if "points" in p:
                out[pts_key][pid] = p["points"]
            if p.get("stats"):
                out[st_key][pid] = p["stats"]
    return out


def _normalize_snapshot_ids(snap: dict, name_to_ucid: dict, live_names: set) -> dict:
    """Re-key every snapshot section by the CURRENT player ID, in memory,
    every cycle. A no-op in the steady state; it converts pre-12.2.0
    name-keyed files on the fly and moves a baseline to a UCID learned
    mid-day (otherwise the player's whole total would count as "today").
    When several keys map to one ID: the ID's own key wins; else entries
    under names active in the save are summed; else the largest is kept
    (pre-12.2.0 renames left stale duplicate copies).
    """
    def _merge(section: dict, is_stats: bool) -> dict:
        grouped: dict = {}
        for key, val in (section or {}).items():
            grouped.setdefault(_player_id(key, name_to_ucid), []).append((key, val))
        out: dict = {}
        for pid, entries in grouped.items():
            own = [v for k, v in entries if k == pid]
            live = [v for k, v in entries if k in live_names]
            if own:
                out[pid] = own[0]
            elif live:
                if is_stats:
                    acc: dict = {}
                    for st in live:
                        for k, v in st.items():
                            acc[k] = acc.get(k, 0) + v
                    out[pid] = acc
                else:
                    out[pid] = sum(live)
            else:
                if is_stats:
                    acc = {}
                    for _, st in entries:
                        for k, v in st.items():
                            acc[k] = max(acc.get(k, 0), v)
                    out[pid] = acc
                else:
                    out[pid] = max(v for _, v in entries)
        return out

    new = dict(snap)
    for key in ("snapshot", "last_daily", "carry_over"):
        new[key] = _merge(snap.get(key), is_stats=False)
    for key in ("stats_snapshot", "last_daily_stats", "stats_carry_over"):
        new[key] = _merge(snap.get(key), is_stats=True)
    names: dict = {}
    for key, name in (snap.get("names") or {}).items():
        names[_player_id(key, name_to_ucid)] = name
    for key in list((snap.get("snapshot") or {})) + list((snap.get("stats_snapshot") or {})):
        pid = _player_id(key, name_to_ucid)
        if not _is_ucid(key) and (pid not in names or key in live_names):
            names[pid] = key
    new["names"] = names
    return new


def _points_delta(current: dict, snapshot: dict, carry_over: dict) -> dict:
    """Today's points per ID: (current - snapshot, never negative) + carry_over;
    only IDs with something to show."""
    out = {}
    for pid, cur in current.items():
        delta = max(0, cur - snapshot.get(pid, 0)) + carry_over.get(pid, 0)
        if delta > 0:
            out[pid] = delta
    for pid, carried in carry_over.items():
        if pid not in out and carried > 0:
            out[pid] = carried
    return out


def _copy_stats(stats: dict) -> dict:
    return {pid: dict(s) for pid, s in stats.items()}


def _by_display_name(by_id: dict, players: dict, names_by_id: dict,
                     ucid_to_rank_name: dict | None = None) -> dict:
    """Re-key an ID-keyed dict by the name the tables use: the
    Foothold_Ranks.lua name for that UCID, else the save-file name, else the
    ID. Pass ucid_to_rank_name when converting several dicts in a row.
    """
    if ucid_to_rank_name is None:
        ucid_to_rank_name = {d.get("ucid"): n for n, d in players.items() if d.get("ucid")}
    out: dict = {}
    for pid, val in (by_id or {}).items():
        name = ucid_to_rank_name.get(pid) or names_by_id.get(pid) or pid
        if name in out:
            if isinstance(val, dict):
                merged = dict(out[name])
                for k, v in val.items():
                    merged[k] = merged.get(k, 0) + v
                out[name] = merged
            else:
                out[name] = out[name] + val
        else:
            out[name] = dict(val) if isinstance(val, dict) else val
    return out


# ── Server selection for /fh_report commands ──────────────────────────────────
class _FHServerTransformer(utils.ServerTransformer):
    """DCSServerBot's ServerTransformer (public server names, hides
    unregistered servers, honours managed_by, pre-selects the channel's
    server), filtered to servers that have an Fh_Report block.
    """

    async def autocomplete(self, interaction: discord.Interaction,
                           current: str) -> list[app_commands.Choice[str]]:
        choices = await super().autocomplete(interaction, current)
        plugin = getattr(interaction.command, "binding", None)
        if plugin is None or not hasattr(plugin, "_has_block"):
            return choices

        def _is_fh(name: str) -> bool:
            srv = interaction.client.servers.get(name)
            return bool(srv) and plugin._has_block(srv)

        filtered = [c for c in choices if _is_fh(c.value)]
        if filtered or current:
            return filtered

        # super() short-circuits empty input to the channel's server; if that one
        # has no Fh_Report block, list every eligible server instead.
        is_admin = self.is_admin(interaction)
        out: list[app_commands.Choice[str]] = []
        for name, srv in interaction.client.servers.items():
            if srv.status == Status.UNREGISTERED:
                continue
            if not plugin._has_block(srv):
                continue
            if (is_admin and srv.locals.get('managed_by') and
                    not utils.check_roles(srv.locals.get('managed_by'), interaction.user)):
                continue
            out.append(app_commands.Choice(name=name, value=name))
            if len(out) == 25:
                break
        return out


# ── Optional private hook ─────────────────────────────────────────────────────
def _load_hook():
    hook_path = os.path.join(os.path.dirname(__file__), "fh_hook.py")
    if not os.path.exists(hook_path):
        return None, False
    try:
        spec = importlib.util.spec_from_file_location("fh_hook", hook_path)
        mod  = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod, True
    except Exception as e:
        log.warning(f"Fh_Report: fh_hook load error: {e}")
        return None, False

_fh_hook, _HAS_HOOK = _load_hook()

def _bool_cfg(value) -> bool:
    """Read a boolean config value with backward compatibility.
    Accepts: true/false (YAML bool), 1/0 (legacy int), "true"/"false" (string).
    Returns True/False."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    if isinstance(value, str):
        return value.lower() in ("true", "1", "yes")
    return False


def _updates_enabled(cfg: dict) -> bool:
    """enable_updates (default true) switches an instance on or off."""
    if cfg.get("enable_updates") is not None:
        return _bool_cfg(cfg["enable_updates"])
    return True


_tz_warned: set[str] = set()


def _tz_from_name(name) -> tzinfo:
    """Time zone for the daily reset: an IANA name (Europe/Madrid, which
    follows summer/winter time by itself), else UTC. An unknown name falls
    back to UTC with one warning."""
    name = str(name or "").strip()
    if not name or name.lower() in ("utc", "gmt", "z"):
        return timezone.utc
    try:
        return ZoneInfo(name)
    except Exception:
        if name not in _tz_warned:
            _tz_warned.add(name)
            log.warning(f"Fh_Report: time zone {name!r} (from the Scheduler plugin) is not a known "
                        f"time zone name (like Europe/Madrid) — using UTC.")
        return timezone.utc


_HHMM = re.compile(r"^(\d{1,2}):(\d{2})$")
_hhmm_warned: set[str] = set()


def _parse_hhmm(value, default: tuple[int, int] = (0, 0)) -> tuple[int, int]:
    """(hour, minute) from a daily reset setting. Accepts "8:30", "08:30",
    "15:30"; a plain number is an hour (4 or "4" = 4:00, the pre-HH:MM
    format). An integer of 60 or more is how a YAML 1.1 reader (PyYAML) turns an
    unquoted H:MM into minutes of the day (8:30 -> 510), so it is read back as
    that; DCSServerBot's own reader (ruamel) keeps it as text. Anything else
    falls back to `default` with one warning."""
    parsed = None
    if isinstance(value, bool) or value is None or value == "":
        parsed = None if isinstance(value, bool) else default
    elif isinstance(value, int):
        if 0 <= value <= 23:
            parsed = (value, 0)
        elif 60 <= value <= 1439:
            parsed = divmod(value, 60)
    elif isinstance(value, str):
        text = value.strip()
        m = _HHMM.match(text)
        if m:
            parsed = (int(m.group(1)), int(m.group(2)))
        elif text.isdigit():
            parsed = (int(text), 0)
    if parsed is None or not (0 <= parsed[0] <= 23 and 0 <= parsed[1] <= 59):
        key = repr(value)
        if key not in _hhmm_warned:
            _hhmm_warned.add(key)
            log.warning(f"Fh_Report: daily reset time {value!r} is not a valid HH:MM (00:00 to 23:59, "
                        f"for example 8:30) — using {default[0]}:{default[1]:02d}.")
        return default
    return parsed


def _reset_time_today(cfg: dict, tz: tzinfo = timezone.utc) -> tuple[int, int]:
    """(hour, minute) of today's daily reset: daily_reset_hour, overridden by
    today's entry in daily_reset_schedule (today is the weekday in the reset
    time zone `tz`)."""
    base = _parse_hhmm(cfg.get("daily_reset_hour"))
    schedule = cfg.get("daily_reset_schedule") or {}
    today = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")[datetime.now(timezone.utc).astimezone(tz).weekday()]
    return _parse_hhmm(schedule[today], base) if today in schedule else base


# Blue-on-blue (BoB) = friendly fire as DCS reported it (DCSSB Mission
# Statistics, `missionstats`): a player's hit or kill on a unit of their own
# coalition. Every hit is its own event, so hits are grouped into one incident
# per attacker + victim + minute (the same grouping Punishment uses), and a
# group is dropped when a kill of that victim type landed in that minute,
# so a kill is never counted twice. BoB = destroyed (kills) + damaged (hits).
BNB_LABELS = {"kill": "Team kill", "hit": "Friendly fire"}
BNB_RECENT = 10   # latest incidents listed in /fh_report player (the embed says when partial)
PU_LABELS = {
    "kill":           "Team kill",
    "collision_kill": "Collision kill",
    "friendly_fire":  "Friendly fire",
    "collision_hit":  "Collision hit",
    "reslot":         "Reslot when shot at",
}

_BNB_INCIDENTS_SQL = """
WITH ff AS (
    SELECT m.init_id, m.event, m.target_id, m.target_type, m.time, s.server_name
    FROM missionstats m JOIN missions s ON s.id = m.mission_id
    WHERE m.event IN ('S_EVENT_KILL', 'S_EVENT_HIT')
      AND m.init_id = ANY(%(ucids)s)
      AND m.init_side = m.target_side AND m.init_side <> '0'
      AND m.target_id IS DISTINCT FROM m.init_id
), incidents AS (
    SELECT init_id, server_name, time, 'kill' AS kind, target_id, target_type
    FROM ff WHERE event = 'S_EVENT_KILL'
    UNION ALL
    SELECT h.init_id, h.server_name, MIN(h.time), 'hit', MAX(h.target_id), MAX(h.target_type)
    FROM ff h
    WHERE h.event = 'S_EVENT_HIT'
      AND NOT EXISTS (SELECT 1 FROM ff k
                      WHERE k.event = 'S_EVENT_KILL' AND k.init_id = h.init_id
                        AND k.target_type IS NOT DISTINCT FROM h.target_type
                        AND date_trunc('minute', k.time) = date_trunc('minute', h.time))
    GROUP BY h.init_id, h.server_name, COALESCE(h.target_id, h.target_type, ''),
             date_trunc('minute', h.time)
)
"""


def _day_start(cfg: dict, now: datetime | None = None, tz: tzinfo = timezone.utc) -> datetime:
    """Most recent daily reset instant (as an aware UTC datetime): today's
    reset hour if it has passed, else yesterday's (each day with its own
    daily_reset_schedule hour), the hours being in the reset time zone `tz`."""
    now = (now or datetime.now(timezone.utc)).astimezone(tz)
    schedule = cfg.get("daily_reset_schedule") or {}
    default  = _parse_hhmm(cfg.get("daily_reset_hour"))
    names    = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

    def at(day: datetime) -> datetime:
        name = names[day.weekday()]
        hour, minute = _parse_hhmm(schedule[name], default) if name in schedule else default
        return day.replace(hour=hour, minute=minute, second=0, microsecond=0)

    start = at(now)
    return (start if now >= start else at(now - timedelta(days=1))).astimezone(timezone.utc)


def _parse_utc(text) -> datetime | None:
    try:
        return datetime.strptime(str(text), "%Y-%m-%dT%H:%M:%S") if text else None
    except ValueError:
        return None


async def _resolve_saves_dir(server, cfg: dict) -> str:
    """Configured saves_dir, else <missions dir>/Saves (same as Pretense)."""
    return cfg.get("saves_dir") or os.path.join(await server.get_missions_dir(), "Saves")


# ── Plugin class ──────────────────────────────────────────────────────────────

class Fh_Report(Plugin):
    """DCSServerBot plugin — posts Foothold campaign status to Discord.
    Supports multiple server instances defined in fh_report.yaml.
    Uses server.node.read_file() so it works transparently in multi-node
    clusters — only the Master runs plugin code; files are fetched from
    agent nodes via the DCSSB RPC bus, exactly like the Pretense plugin."""

    def __init__(self, bot: DCSServerBot, eventlistener: Type[TEventListener] = None):
        super().__init__(bot, eventlistener)
        self._message_ids: dict = {}
        self._layout_cycle_index: dict = {}
        self._last_update: float = 0.0
        self._post_sleep_reset: bool = False
        self._cycle_punishment: dict | None = None
        self._campaign_absent: set[str] = set()      # saves_dirs whose Foothold save was missing last cycle
        self._campaign_watch: dict[str, dict] = {}   # saves_dir -> last intact reading, for _update_session
        self._message_ids_file: str = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "message_ids.json"
        )
        self._last_maps: dict[str, str] = {}          # last known map per instance
        self._map_probed: set[str] = set()            # instances whose mission file was already tried
        self._base_interval = 300                     # update_interval of DEFAULT
        self._beat = 300                              # the loop's step: the shortest interval in the file
        self._beats_left: dict[str, int] = {}         # per server: beats to skip before its next update
        self._last_status: dict[str, object] = {}     # per server: the status seen on the previous beat
        self._reset_handled: dict[str, datetime] = {}  # per server: the last daily reset already refreshed


    # ── Lifecycle ─────────────────────────────────────────────────────────

    async def install(self) -> None:
        """No database tables needed for this plugin."""
        pass

    async def cog_load(self) -> None:
        await super().cog_load()
        self._message_ids = self._load_message_ids()
        raw      = self.locals or {}
        interval, interval_warning = _validated_update_interval(raw)
        if interval_warning:
            self.log.warning(interval_warning)
        self._base_interval = interval
        self._beat = self._compute_beat(interval)
        self._beats_left = {}
        self._last_status = {}
        self._reset_handled = {}
        self.updater.change_interval(seconds=self._beat)
        utils.safe_start(self.updater)

    async def cog_unload(self) -> None:
        await utils.safe_cancel(self.updater)
        await super().cog_unload()

    # ── Update intervals: the shortest one sets the beat ───────────────────

    def _compute_beat(self, base: int) -> int:
        """The loop's step: the shortest valid update_interval found in the file
        (DEFAULT's, or any server block's, flat or under a node)."""
        beat = base
        for key, val in (self.locals or {}).items():
            if key == "DEFAULT" or not isinstance(val, dict):
                continue
            for block in [val] + [c for c in val.values() if isinstance(c, dict)]:
                n = _parse_interval(block.get("update_interval"))
                if n:
                    beat = min(beat, n)
        return beat

    def _interval_for(self, server) -> int:
        """The server's own update_interval if its block sets one (DEFAULT's is then
        ignored for it), else DEFAULT's. An invalid own value falls back to DEFAULT's."""
        block = self._block_for(server)[0] or {}
        if block.get("update_interval") is None:
            return self._base_interval
        n = _parse_interval(block["update_interval"])
        if n is None:
            key = _server_key(server)
            if key not in _interval_warned:
                _interval_warned.add(key)
                self.log.warning(
                    f"Fh_Report [{key}]: update_interval ({block['update_interval']!r}) is not a whole number "
                    f"of seconds above zero - using the DEFAULT's ({self._base_interval}s).")
            return self._base_interval
        return n

    def _latest_reset(self, server) -> datetime | None:
        """The most recent daily reset instant of this server (UTC), from its
        daily_reset_hour / daily_reset_schedule and the Scheduler's time zone."""
        try:
            return _day_start(self._merged_cfg(server), tz=self._reset_tz(server))
        except Exception as e:
            self.log.debug(f"Fh_Report: daily reset time not available: {e!r}")
            return None

    def _due_servers(self, servers: list) -> list:
        """The servers to update on this beat. Only a running mission changes
        Foothold's files, so a server whose mission is paused, loading or stopped
        is left alone; it is still updated once on the first beat after the plugin
        loads, once each time its status changes (the last state when it pauses or
        stops, the fresh one when it runs again) and once when its daily reset
        time passes, so the day is closed on time even with nobody playing. A
        running server that wants a longer interval than the beat is updated every
        ceil(interval / beat) beats; a server seen for the first time at once."""
        due = []
        for server in servers:
            key = _server_key(server)
            status = server.status
            previous = self._last_status.get(key)
            self._last_status[key] = status
            changed = previous is None or previous != status
            if changed:
                self._beats_left[key] = 0
            if status != Status.RUNNING:
                if changed:
                    due.append(server)
                    latest = self._latest_reset(server)
                    if latest:
                        self._reset_handled[key] = latest
                else:
                    latest = self._latest_reset(server)
                    if latest and latest > self._reset_handled.get(key, latest):
                        self._reset_handled[key] = latest
                        due.append(server)
                continue
            left = self._beats_left.get(key, 0)
            if left > 0:
                self._beats_left[key] = left - 1
                continue
            due.append(server)
            self._beats_left[key] = max(1, -(-self._interval_for(server) // max(1, self._beat))) - 1
        return due

    # ── Message IDs persistence (JSON file, no DB) ─────────────────────────

    def _load_message_ids(self) -> dict:
        if os.path.exists(self._message_ids_file):
            try:
                with open(self._message_ids_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except (ValueError, OSError):
                pass
        return {}

    def _save_message_ids(self) -> None:
        try:
            with open(self._message_ids_file, "w", encoding="utf-8") as f:
                json.dump(self._message_ids, f, indent=2)
        except OSError as e:
            self.log.error(f"Fh_Report: could not save message IDs: {e}")

    # ── Map of the mission (from DCSServerBot, kept in memory) ─────────────

    async def _known_map(self, server, show: bool = True) -> str | None:
        """The map DCSServerBot reports for this instance's mission, kept in
        memory so it's still shown while no mission is loaded; replaced as soon
        as DCSSB reports another. If nothing is known (e.g. the bot restarted
        while the server is stopped), it is read ONCE from the mission file —
        that opens the whole .miz, so never on every cycle. None if unknown."""
        instance_name = _server_key(server)
        try:
            current = getattr(getattr(server, "current_mission", None), "map", None)
        except Exception:
            current = None
        if isinstance(current, str) and current.strip():
            self._last_maps[instance_name] = current.strip()
        elif show and instance_name not in self._last_maps and instance_name not in self._map_probed:
            self._map_probed.add(instance_name)
            try:
                theatre = await asyncio.wait_for(server.get_current_mission_theatre(), MAP_PROBE_TIMEOUT)
            except Exception as e:
                self.log.debug(f"Fh_Report [{instance_name}]: could not read the map from the mission file: {e!r}")
                theatre = None
            if isinstance(theatre, str) and theatre.strip():
                self._last_maps[instance_name] = theatre.strip()
        return self._last_maps.get(instance_name)

    # ── Core update task ──────────────────────────────────────────────────

    @tasks.loop(seconds=300)
    async def updater(self):
        now = time.monotonic()
        # Anti-burst: detect PC suspension (elapsed >> interval)
        interval = self.updater.seconds or 300
        elapsed  = now - self._last_update if self._last_update > 0 else interval
        if self._last_update > 0 and elapsed > interval * 1.5:
            self._last_update = now
            self._post_sleep_reset = True
            return
        if self._post_sleep_reset and elapsed < 10:
            return
        self._post_sleep_reset = False
        self._last_update = now

        raw          = self.locals or {}
        self._cycle_punishment = None
        self._migrate_message_ids()
        self._warn_config_mismatches(raw)

        servers = self._due_servers(self._configured_servers())
        stagger_seconds = _server_stagger_seconds(interval, len(servers))
        processed_server_count = 0
        for server in servers:
            try:
                cfg = self._merged_cfg(server)
                if processed_server_count > 0:
                    await asyncio.sleep(stagger_seconds)
                processed_server_count += 1
                await self._update_server(server, cfg)
            except Exception as e:
                self.log.error(
                    f"Fh_Report [{_server_key(server)}]: unexpected error: {e}", exc_info=True
                )

    @updater.before_loop
    async def before_updater(self):
        await self.bot.wait_until_ready()

    def _resolve_report_layout(self, server_name: str, cfg: dict) -> tuple[str, dict]:
        """(report_layout, points_detail) from config. report_layout may be a
        comma-separated rotation ("DP, SR"): one group per update cycle, always
        restarting at the first after a reload. points_detail_D/S/R apply to
        every group; a role without its own key shows only its own value.
        Legacy keys are converted once in the YAML by migrate_config.py.
        """
        groups = [g.strip() for g in str(cfg.get("report_layout") or "R").split(",") if g.strip()]
        if not groups:
            groups = ["R"]
        if len(groups) == 1:
            layout = groups[0]
        else:
            idx = self._layout_cycle_index.get(server_name, 0)
            layout = groups[idx % len(groups)]
            self._layout_cycle_index[server_name] = (idx + 1) % len(groups)

        detail = {}
        for role in ("D", "S", "R"):
            raw = cfg.get(f"points_detail_{role}")
            if raw is not None:
                detail[role] = str(raw).strip().upper()
        return layout, detail

    def _get_daily_file(self, saves_dir: str) -> str:
        """Return path to fhr_daily_snapshot.json cache file."""
        return _fhr_path(saves_dir, "daily_snapshot.json")

    def _get_history_file(self, saves_dir: str) -> str:
        """Return path to fhr_daily_history.json — the Podium's record of each
        day's top 50, keyed by date (YYYY-MM-DD)."""
        return _fhr_path(saves_dir, "daily_history.json")

    async def _load_daily_history(self, saves_dir: str, node, cleanup: bool = False) -> dict:
        """fhr_daily_history.json: {date: [event, ...]}, each event
        {"campaign_restart": bool, "top": [{"name", "points"[, "ucid"]}]}. A date
        can hold more than one event. {} if missing.
        """
        return await _read_fhr_json(node, saves_dir, "daily_history.json", cleanup)

    async def _save_daily_history(self, saves_dir: str, data: dict, node) -> None:
        """Write fhr_daily_history.json via write_bytes_to_node (newest date first,
        for readability only).
        """
        path = self._get_history_file(saves_dir)
        await _ensure_fhc_dir(node, saves_dir)
        # Newest date first, for humans only — readers re-sort.
        sorted_data = dict(sorted(data.items(), key=lambda kv: kv[0], reverse=True))
        await write_bytes_to_node(
            node, path,
            json.dumps(sorted_data, indent=2).encode("utf-8"),
            log=self.log
        )

    async def _load_daily_snapshot(self, saves_dir: str, node, cleanup: bool = False) -> dict:
        """fhr_daily_snapshot.json exactly as stored (see _pack_daily_snapshot);
        callers unpack it with _unpack_daily_snapshot. {} if missing.
        """
        return await _read_fhr_json(node, saves_dir, "daily_snapshot.json", cleanup)

    async def _save_daily_snapshot(self, saves_dir: str, data: dict, node) -> None:
        """Write fhr_daily_snapshot.json via write_bytes_to_node."""
        path = self._get_daily_file(saves_dir)
        await _ensure_fhc_dir(node, saves_dir)
        await write_bytes_to_node(
            node, path,
            json.dumps(data, indent=2).encode("utf-8"),
            log=self.log
        )

    def _identify_players(self, snap_unpacked: dict, campaign_stats: dict, session_stats_raw: dict,
                          players: dict, name_to_ucid_native: dict) -> tuple[dict, dict, dict, dict]:
        """`snap_unpacked` is fhr_daily_snapshot.json after _unpack_daily_snapshot.
        Full name -> UCID map (snapshot history < Foothold_Ranks.lua < save
        file's native UCIDs) and the save data regrouped by player ID. Returns
        (campaign_by_id, session_by_id, names_by_id, name_to_ucid).
        """
        history_map = snap_unpacked.get("name_to_ucid") or {}
        ranks_map   = {n: d.get("ucid") for n, d in players.items() if d.get("ucid")}
        name_to_ucid = {**history_map, **ranks_map, **(name_to_ucid_native or {})}
        campaign_by_id, session_by_id, names_by_id = _group_by_player_id(
            campaign_stats, session_stats_raw, name_to_ucid)
        return campaign_by_id, session_by_id, names_by_id, name_to_ucid

    async def _compute_daily_points(self, saves_dir: str, campaign_stats: dict,
                              session_stats_raw: dict, reset_hour: int | tuple, node,
                              persistence_filename: str | None = None,
                              name_to_ucid: dict | None = None,
                              names_by_id: dict | None = None,
                              live_names: set | None = None,
                              snap: dict | None = None,
                              persist: bool = True,
                              snap_unpacked: dict | None = None,
                              tz: tzinfo = timezone.utc) -> tuple[dict, dict, bool]:
        """Today's points and stat deltas per player ID, against the baseline
        snapshot taken at reset_hour (an hour, or an (hour, minute) pair) in time zone `tz` (the server's Scheduler
        timezone, UTC without one). Returns (daily_pts, daily_stats,
        campaign_restarted).

        Inputs are already grouped by player ID (see _group_by_player_id), so
        renames need no special handling. `snap` is the snapshot already read
        this cycle. persist=False computes in memory only, so read-only callers
        (/fh_report player) never race the updater, the only writer.

        Only the real reset_hour rollover closes a day and records it for the
        Podium. A mid-day mission/map change — persistence filename changed,
        every snapshot player vanished, or points and kills both dropped in
        place — instead moves today's totals into carry_over and rebases the
        snapshot, so daily = (current - snapshot) + carry_over continues
        seamlessly. To reset the daily counters manually, delete
        saves_dir/.fhc/fhr_daily_snapshot.json — and daily_snapshot.json too if
        it's still there (a missing snapshot starts at 0).
        """
        now_local = datetime.now(timezone.utc).astimezone(tz)
        today_str = now_local.strftime("%Y-%m-%d")

        if snap is None:
            snap = await self._load_daily_snapshot(saves_dir, node, cleanup=persist)
        snap_on_disk = snap          # exactly what's on disk, to skip no-op writes below
        snap = snap_unpacked if snap_unpacked is not None else _unpack_daily_snapshot(snap)
        if snap:
            snap = _normalize_snapshot_ids(snap, name_to_ucid or {}, live_names or set())
        snap_date           = snap.get("date", "")
        snapshot            = snap.get("snapshot", {})
        stats_snapshot      = snap.get("stats_snapshot", {})
        last_daily_saved    = snap.get("last_daily", {})
        last_daily_stats_saved = snap.get("last_daily_stats", {})
        carry_over          = snap.get("carry_over", {})
        stats_carry_over    = snap.get("stats_carry_over", {})
        last_persistence_fn = snap.get("persistence_filename", "")
        name_to_ucid_snapshot = snap.get("name_to_ucid", {})
        names_snapshot        = snap.get("names", {})
        names_by_id           = names_by_id or {}
        # Stat categories ever seen, persisted. Derived from stats_snapshot alone,
        # every category vanished from the Daily card after a mid-day mission swap
        # (the rebased snapshot is empty until someone flies).
        known_stat_keys = set(snap.get("known_stat_keys", []))

        # ── Mid-day mission/map reset detection (never a Podium event) ─────
        filename_changed = bool(last_persistence_fn) and bool(persistence_filename) and \
                            last_persistence_fn != persistence_filename
        common_with_snapshot = set(snapshot) & set(campaign_stats)
        data_vanished = bool(snapshot) and not common_with_snapshot

        # ── In-place campaign reset: points AND kills both dropped ─────────
        campaign_restarted = False
        if common_with_snapshot:
            snap_pts_sum = sum(snapshot.get(n, 0) for n in common_with_snapshot)
            cur_pts_sum  = sum(campaign_stats.get(n, 0) for n in common_with_snapshot)
            points_dropped = snap_pts_sum > 0 and cur_pts_sum < snap_pts_sum * 0.5

            def _kill_sum(stats_dict, names):
                total = 0
                for n in names:
                    s = stats_dict.get(n, {})
                    total += s.get("Air", 0) + s.get("Ground Units", 0)
                return total

            snap_kills_sum = _kill_sum(stats_snapshot, common_with_snapshot)
            cur_kills_sum  = _kill_sum(session_stats_raw, common_with_snapshot)
            kills_dropped  = snap_kills_sum > 0 and cur_kills_sum < snap_kills_sum

            campaign_restarted = points_dropped and kills_dropped

        mission_reset = filename_changed or data_vanished or campaign_restarted

        # ── Real calendar-day reset (the only thing that closes a day) ─────
        reset_h, reset_m = reset_hour if isinstance(reset_hour, tuple) else (int(reset_hour), 0)
        reset_time     = now_local.replace(hour=reset_h, minute=reset_m, second=0, microsecond=0)
        first_run      = not snap_date
        date_reset_due = (not first_run) and snap_date != today_str and now_local >= reset_time

        if first_run:
            # No prior day to close or carry over — start completely fresh.
            snapshot         = dict(campaign_stats)
            stats_snapshot   = _copy_stats(session_stats_raw)
            carry_over       = {}
            stats_carry_over = {}

        elif date_reset_due:
            reason = " (a mid-day mission/map reset was also detected and is folded in)" if mission_reset else ""
            self.log.debug(f"Fh_Report: daily reset for {saves_dir} at {reset_h:02d}:{reset_m:02d} {tz}{reason}")

            # Close the day for the Podium from the current snapshot/carry_over (which
            # already include earlier mid-day swaps); last_daily_saved covers a swap
            # landing in this same cycle.
            closing_daily = _points_delta(campaign_stats, snapshot, carry_over)
            for name, val in last_daily_saved.items():
                if name not in closing_daily and val > 0:
                    closing_daily[name] = val

            if closing_daily and persist:
                top_list = sorted(closing_daily.items(), key=lambda kv: kv[1], reverse=True)[:50]
                history    = await self._load_daily_history(saves_dir, node, cleanup=True)
                # Store the UCID too, so the Podium can show the CURRENT name/rank later.
                _top_entries = []
                for pid, p in top_list:
                    _entry = {"name": names_by_id.get(pid) or names_snapshot.get(pid) or pid,
                              "points": p}
                    if _is_ucid(pid):
                        _entry["ucid"] = pid
                    _top_entries.append(_entry)
                # Backfill UCIDs of pre-12.1.1 entries in the same write, so the Podium
                # never needs the (capped) alias history.
                _n2u = {**name_to_ucid_snapshot, **(name_to_ucid or {})}
                for _events in history.values():
                    for _event in _events:
                        for _e in _event.get("top") or []:
                            if not _e.get("ucid") and _n2u.get(_e.get("name")):
                                _e["ucid"] = _n2u[_e["name"]]
                history.setdefault(snap_date, []).append({
                    "campaign_restart": False,
                    "top": _top_entries,
                })
                await self._save_daily_history(saves_dir, history, node)

            snapshot         = dict(campaign_stats)
            stats_snapshot   = _copy_stats(session_stats_raw)
            carry_over       = {}
            stats_carry_over = {}

        elif mission_reset:
            # Mid-day mission/map change (never a Podium event): current data is the
            # NEW file, so today's earnings come from last_daily_saved. Fold them into
            # carry_over and rebase the snapshot onto the new file.
            self.log.debug(
                f"Fh_Report: mid-day mission/map reset detected for {saves_dir} "
                f"(filename_changed={filename_changed}, data_vanished={data_vanished}, "
                f"campaign_restarted={campaign_restarted}) — carrying today's totals "
                f"forward, no Podium entry."
            )
            new_carry_over = dict(carry_over)
            for name, val in last_daily_saved.items():
                if val > 0:
                    new_carry_over[name] = max(new_carry_over.get(name, 0), val)
            carry_over = new_carry_over

            new_stats_carry_over = _copy_stats(stats_carry_over)
            for name, stats in last_daily_stats_saved.items():
                merged = dict(new_stats_carry_over.get(name, {}))
                for key, val in stats.items():
                    if val > merged.get(key, 0):
                        merged[key] = val
                if merged:
                    new_stats_carry_over[name] = merged
            stats_carry_over = new_stats_carry_over

            snapshot       = dict(campaign_stats)
            stats_snapshot = _copy_stats(session_stats_raw)

        # ── Calculate today's point delta for each player ──────────────────
        # (snapshot/carry_over above already reflect any resets/carries
        # that happened this cycle, so this is a single, uniform formula.)
        daily = _points_delta(campaign_stats, snapshot, carry_over)

        # ── Today's stat deltas ──────────────────────────────────────────────
        # Only for keys already tracked at the last snapshot: a brand-new key
        # would otherwise show its whole cumulative value as today's.
        tracked_keys_in_snapshot = set(known_stat_keys)
        for _stats in stats_snapshot.values():
            tracked_keys_in_snapshot.update(_stats.keys())

        daily_stats = {}
        for name, current_stats in session_stats_raw.items():
            baseline_stats = stats_snapshot.get(name, {})
            carried_stats  = stats_carry_over.get(name, {})
            delta_stats = dict(carried_stats)
            for key, current_val in current_stats.items():
                if key not in tracked_keys_in_snapshot:
                    continue
                base_val = baseline_stats.get(key, 0)
                d = current_val - base_val
                if d > 0:
                    delta_stats[key] = delta_stats.get(key, 0) + d
            if delta_stats:
                daily_stats[name] = delta_stats
        for name, carried_stats in stats_carry_over.items():
            if name not in daily_stats and carried_stats:
                daily_stats[name] = dict(carried_stats)

        # Categories seen now are trusted from the next cycle on.
        for _stats in session_stats_raw.values():
            known_stat_keys.update(_stats.keys())
        for _stats in stats_snapshot.values():
            known_stat_keys.update(_stats.keys())

        # Persisted every cycle: last_daily and carry-over are what survive a
        # mission swap; the filename detects the next one.
        new_snap = {
            "date":                 today_str if (first_run or date_reset_due) else (snap_date or today_str),
            "snapshot":             snapshot,
            "stats_snapshot":       stats_snapshot,
            "last_daily":           daily,
            "last_daily_stats":     daily_stats,
            "carry_over":           carry_over,
            "stats_carry_over":     stats_carry_over,
            "persistence_filename": persistence_filename or last_persistence_fn,
            "name_to_ucid":         {**name_to_ucid_snapshot, **(name_to_ucid or {})},
            "names":                {**names_snapshot, **names_by_id},
            "known_stat_keys":      sorted(known_stat_keys),
        }
        # Only write when something actually changed — with nobody earning
        # points (e.g. an empty server) every cycle would otherwise rewrite
        # an identical file, over the network for remote nodes.
        new_snap = _pack_daily_snapshot(new_snap, live_names)
        if persist and new_snap != snap_on_disk:
            await self._save_daily_snapshot(saves_dir, new_snap, node)

        return daily, daily_stats, campaign_restarted

    def _reset_tz(self, server) -> tzinfo:
        """Time zone of this server's daily reset: the `timezone` the Scheduler
        plugin has for it (DCSServerBot's own setting), UTC when it has none or
        the Scheduler plugin is not there."""
        try:
            name = (self.get_config(server, plugin_name="scheduler") or {}).get("timezone")
        except Exception:
            name = None
        return _tz_from_name(name)

    async def _reset_moment(self, server, seen: datetime | None, now: datetime) -> datetime:
        """When a campaign reset noticed at `now` most likely happened: Foothold
        restarts the mission seconds after a reset, so the first mission start
        DCSServerBot recorded since the campaign was last seen intact. `now`
        when there is none, or the window is too wide to say."""
        if seen and timedelta(0) < now - seen <= _SESSION_WINDOW:
            try:
                async with self.apool.connection() as conn:
                    async with conn.cursor() as cur:
                        await cur.execute(
                            "SELECT MIN(mission_start) FROM missions "
                            "WHERE server_name = %s AND mission_start > %s AND mission_start <= %s",
                            (server.name, seen, now))
                        row = await cur.fetchone()
                        if row and row[0]:
                            return row[0]
            except Exception as e:
                self.log.debug(f"Fh_Report: mission start not available: {e}")
        return now

    async def _update_session(self, server, saves_dir: str, node, source_node,
                              persistence_file: str, points: dict, kills: dict, cfg: dict | None = None) -> datetime:
        """Notice campaign resets, keep .fhc/fhr_session.json current, and
        return the session start (naive UTC). `points` / `kills` are the
        campaign points and Air+Ground kills per player ID. See the comment above."""
        basename = os.path.basename(persistence_file)
        raw = await _read_fhr_json(node, saves_dir, "session.json")
        old = _session_migrate(raw)
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        cur = {"points": dict(points), "kills": dict(kills)}
        saves_key = _saves_key(source_node, saves_dir)
        absent = saves_key in self._campaign_absent
        self._campaign_absent.discard(saves_key)

        watch = self._campaign_watch.get(saves_key)
        if watch is None and old and old.get("file") == basename and old.get("base"):   # after a bot restart
            watch = {**old["base"], "seen": _parse_utc(old.get("seen")), "pending": False}
        seen = (watch or {}).get("seen") or _parse_utc((old or {}).get("seen"))

        reset = False
        if not old:
            st = {"file": basename, "start": now.strftime(_SESSION_ISO), "kind": "first_seen"}
            self.log.debug(f"Fh_Report [{server.instance.name}]: campaign {basename} first seen {st['start']} UTC")
            watch = {**cur, "seen": now, "pending": False}
        elif old.get("file") != basename or absent:       # other map, or the save reappeared
            reset, watch = True, {**cur, "seen": now, "pending": False}
        elif _campaign_looks_reset(watch, cur):
            if watch.get("pending"):                      # second look agrees: it's a reset
                reset, watch = True, {**cur, "seen": now, "pending": False}
            else:                                         # keep the old baseline, look again next cycle
                watch = {**watch, "pending": True}
        else:
            watch = {**cur, "seen": now, "pending": False}
        if reset:
            st = {"file": basename, "kind": "detected",
                  "start": (await self._reset_moment(server, seen, now)).strftime(_SESSION_ISO)}
            self.log.info(f"Fh_Report [{server.instance.name}]: new campaign session ({basename}) from {st['start']} UTC "
                          f"(noticed {now.strftime(_SESSION_ISO)} UTC)")
        elif old:
            st = {k: old[k] for k in ("file", "start", "kind")}
        self._campaign_watch[saves_key] = watch

        stale = not raw or now - (_parse_utc(raw.get("seen")) or now) >= _SESSION_SEEN_EVERY
        if reset or not old or raw.get("start") != st["start"] or raw.get("file") != st["file"] or (stale and not watch["pending"]):
            st.update(seen=(watch["seen"] or now).strftime(_SESSION_ISO),
                      base={"points": watch["points"], "kills": watch["kills"]})
            await _ensure_fhc_dir(source_node, saves_dir)
            await write_bytes_to_node(source_node, _fhr_path(saves_dir, "session.json"),
                                      json.dumps(st, indent=2).encode("utf-8"), log=self.log)
        return _session_start_for(st, cfg or {}, self._reset_tz(server))

    async def _fetch_bnb(self, server, cfg: dict, session: datetime | None, ucids: list) -> dict:
        """Blue-on-blue counts per UCID from DCSSB's Mission Statistics:
        {ucid: {"total", "day", "session"}}. Total is every server and all the
        history kept; day and session only count this server since the last
        daily reset / session start. Empty when the plugin's table isn't there."""
        if not ucids:
            return {}
        day_from = _day_start(cfg, tz=self._reset_tz(server)).replace(tzinfo=None)
        try:
            async with self.apool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(_BNB_INCIDENTS_SQL + """
                        SELECT init_id, COUNT(*),
                               COUNT(*) FILTER (WHERE server_name = %(server)s AND time >= %(day)s),
                               COUNT(*) FILTER (WHERE server_name = %(server)s AND time >= %(session)s)
                        FROM incidents GROUP BY init_id
                    """, {"ucids": list(ucids), "server": server.name, "day": day_from,
                          "session": session or datetime.max})
                    return {r[0]: {"total": int(r[1]), "day": int(r[2]), "session": int(r[3])}
                            for r in await cur.fetchall()}
        except Exception as e:
            self.log.debug(f"Fh_Report: BoB data not available: {e}")
            return {}

    async def _fetch_bnb_detail(self, server, cfg: dict, session: datetime | None, ucid: str,
                                with_penalties: bool = False, with_bnb: bool = True) -> tuple[dict | None, dict | None]:
        """One player's detail for /fh_report player: (bnb, penalties).
        bnb: totals split into destroyed / damaged, today and this session on
        this server, plus the latest incidents. penalties (only when asked):
        what Punishment still holds against the player after decay, by event.
        Each is None when there is nothing or the plugin's table is missing.
        `session` is the campaign session start (naive UTC), or None when unknown."""
        day_from = _day_start(cfg, tz=self._reset_tz(server)).replace(tzinfo=None)
        bnb = penalties = None
        try:
            async with self.apool.connection() as conn:
                async with conn.cursor() as cur:
                    params = {"ucids": [ucid], "server": server.name, "day": day_from,
                              "session": session or datetime.max}
                    try:
                        if not with_bnb:
                            raise LookupError("BoB switched off")
                        await cur.execute(_BNB_INCIDENTS_SQL + """
                            SELECT kind, COUNT(*),
                                   COUNT(*) FILTER (WHERE server_name = %(server)s AND time >= %(day)s),
                                   COUNT(*) FILTER (WHERE server_name = %(server)s AND time >= %(session)s)
                            FROM incidents GROUP BY kind
                        """, params)
                        by_kind = {r[0]: (int(r[1]), int(r[2]), int(r[3])) for r in await cur.fetchall()}
                        if by_kind:
                            await cur.execute(_BNB_INCIDENTS_SQL + """
                                SELECT i.time, i.kind, i.target_id, t.name, i.target_type
                                FROM incidents i LEFT JOIN players t ON t.ucid = i.target_id
                                WHERE i.server_name = %(server)s AND i.time >= %(since)s
                                ORDER BY i.time DESC LIMIT %(limit)s
                            """, {**params, "since": session or datetime.min, "limit": BNB_RECENT + 1})
                            rows = [tuple(r) for r in await cur.fetchall()]   # this server, this session
                            bnb = {
                                "total":     sum(v[0] for v in by_kind.values()),
                                "day":       sum(v[1] for v in by_kind.values()),
                                "session":   sum(v[2] for v in by_kind.values()),
                                "destroyed": by_kind.get("kill", (0, 0, 0))[0],
                                "damaged":   by_kind.get("hit", (0, 0, 0))[0],
                                "recent":    rows[:BNB_RECENT],
                                "more":      len(rows) > BNB_RECENT,
                                "session_known": session is not None,
                            }
                    except LookupError:
                        pass
                    except Exception as e:
                        self.log.debug(f"Fh_Report: BoB detail not available: {e}")
                    if with_penalties:
                        try:
                            await cur.execute(
                                "SELECT event, COUNT(*), SUM(points) FROM pu_events "
                                "WHERE init_id = %s GROUP BY event ORDER BY SUM(points) DESC", (ucid,))
                            rows = [(r[0], int(r[1]), float(r[2])) for r in await cur.fetchall()]
                            if rows:
                                penalties = {"total": sum(r[2] for r in rows), "rows": rows}
                        except Exception as e:
                            self.log.debug(f"Fh_Report: penalties not available: {e}")
        except Exception as e:
            self.log.debug(f"Fh_Report: BoB/penalties connection failed: {e}")
        return bnb, penalties

    async def _fetch_punishment_points(self) -> dict:
        """Fetch total punishment points per UCID from pu_events table."""
        try:
            async with self.apool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute("""
                        SELECT init_id, COALESCE(SUM(points), 0) AS total
                        FROM pu_events
                        WHERE points > 0
                        GROUP BY init_id
                    """)
                    rows = await cur.fetchall()
                    return {row[0]: float(row[1]) for row in rows}
        except Exception as e:
            self.log.debug(f"Fh_Report: punishment points not available: {e}")
            return {}

    async def _update_server(self, server, cfg: dict):
        """Read one instance's Foothold files (through server.node, so remote
        agent nodes work) and post or edit its Discord embed. One update of a
        server at a time: the loop and /fh_report update never overlap.
        """
        locks = self.__dict__.setdefault("_update_locks", {})
        async with locks.setdefault(_server_key(server), asyncio.Lock()):
            await self._update_server_locked(server, cfg)

    async def _update_server_locked(self, server, cfg: dict):

        instance_name = _server_key(server)     # node/instance: unique across the cluster
        hook_name = server.instance.name         # what the optional private hook has always received

        if not _updates_enabled(cfg):
            # Switched-off instance (or a duplicate install elsewhere in the
            # cluster posting to the same channel): no read, no post.
            return

        channel_id    = _single_channel_id(cfg.get("channel_id"))
        if not channel_id:
            self.log.warning(f"Fh_Report [{instance_name}]: channel_id not configured.")
            return

        channel = self.bot.get_channel(int(channel_id))
        if not channel:
            self.log.warning(f"Fh_Report [{instance_name}]: channel {channel_id} not found.")
            return

        saves_dir = await _resolve_saves_dir(server, cfg)

        source_node = server.node
        node = _UpdateReadCache(source_node)
        map_name = await self._known_map(server, _show_map(cfg))

        # One-time-per-instance write self-test — see run_write_self_test()
        # for why this doesn't wait for a real correction to be needed.
        await run_write_self_test(source_node, saves_dir, log=self.log)

        persistence_file = await find_persistence_file(saves_dir, node)
        if not persistence_file:
            await self._post_not_started(server, cfg, channel, channel_id, saves_dir, node, map_name)
            return
        if instance_name in _missing_save_warned:
            _missing_save_warned.discard(instance_name)
            self.log.debug(f"Fh_Report [{instance_name}]: Foothold save found in {saves_dir} — full report resumed.")

        ranks_file    = os.path.join(saves_dir, "Foothold_Ranks.lua")
        ranks_missing = False
        ranks_source  = None
        try:
            ranks_source = await node.read_file(ranks_file)
        except FileNotFoundError:
            self.log.debug(
                f"Fh_Report [{instance_name}]: Foothold_Ranks.lua not found — "
                f"showing zone status without leaderboard."
            )
            ranks_missing = True

        if not ranks_missing:
            # Deduplicate player entries caused by callsign changes before parsing.
            try:
                if await deduplicate_ranks(ranks_file, persistence_file, source_node, ranks_source):
                    node.invalidate(ranks_file)
            except Exception as e:
                self.log.error(f"Fh_Report [{instance_name}]: deduplication error: {e}")

        try:
            excluded_ucids = cfg.get("excluded_ucids") or []
            zones          = await parse_zones(persistence_file, node)
            players        = {} if ranks_missing else await parse_ranks(ranks_file, excluded_ucids, node)
            campaign_stats, session_stats_raw, name_to_ucid_native = await parse_player_stats(persistence_file, node)
        except Exception as e:
            self.log.error(f"Fh_Report [{instance_name}]: error parsing data: {e}")
            return

        # Optional private hook — post-processes players dict
        if _HAS_HOOK:
            try:
                players = _fh_hook.post_process(players, cfg, hook_name, campaign_stats)
            except Exception as e:
                self.log.debug(f"Fh_Report [{instance_name}]: fh_hook.post_process failed: {e}")

        show_punishment   = _bool_cfg(cfg.get("show_punishment"))
        punishment_points = {}
        if show_punishment:
            if self._cycle_punishment is None:   # same query for every server: once per cycle
                self._cycle_punishment = await self._fetch_punishment_points()
            punishment_points = self._cycle_punishment

        # Daily computation is needed for D tables, a "D" in any points_detail, or
        # P (the Podium history is recorded inside it). Checked against the whole
        # rotation string so every group's data stays warm; "none" groups never
        # need it.
        raw_layout = ",".join(g for g in str(cfg.get("report_layout") or "R").strip().upper().split(",")
                              if g.strip() and g.strip() != "NONE")
        needs_daily = ("D" in raw_layout) or ("P" in raw_layout) or (bool(raw_layout) and any(
            "D" in str(cfg.get(f"points_detail_{role}") or "").upper()
            for role in ("D", "S", "R")
        ))
        # Waypoint sorting also needs it: its campaign-restart detection triggers
        # the waypoint cache refresh.
        needs_daily = needs_daily or _bool_cfg(cfg.get("sort_zones_by_waypoint"))
        daily_pts: dict = {}
        daily_stats: dict = {}
        campaign_restarted_now = False
        # Must run even with empty campaign_stats: right after a mission change it's
        # what detects the swap and carries today's points over.
        daily_snap = await self._load_daily_snapshot(saves_dir, node, cleanup=True) if needs_daily else {}
        snap_unpacked = _unpack_daily_snapshot(daily_snap)
        campaign_by_id, session_by_id, names_by_id, name_to_ucid = self._identify_players(
            snap_unpacked, campaign_stats, session_stats_raw, players, name_to_ucid_native)
        live_names = set(campaign_stats) | set(session_stats_raw)
        if needs_daily:
            tz = self._reset_tz(server)
            reset_hour = _reset_time_today(cfg, tz)
            daily_pts, daily_stats, campaign_restarted_now = await self._compute_daily_points(
                saves_dir, campaign_by_id, session_by_id, reset_hour, source_node,
                os.path.basename(persistence_file) if persistence_file else None,
                name_to_ucid, names_by_id, live_names, daily_snap, snap_unpacked=snap_unpacked,
                tz=tz)

        # Campaign session start (always kept up to date: /fh_report player reads it too)
        session = await self._update_session(
            server, saves_dir, node, source_node, persistence_file, campaign_by_id,
            {pid: st.get("Air", 0) + st.get("Ground Units", 0) for pid, st in session_by_id.items()}, cfg)
        bnb = {}
        if _show_bob(cfg) and any(_bool_cfg(cfg.get(k)) for k in ("show_pilot_card", "show_session_card", "show_daily_card")):
            bnb = await self._fetch_bnb(
                server, cfg, session,
                [d["ucid"] for d in players.values() if d.get("ucid")])

        current_layout, current_detail = self._resolve_report_layout(instance_name, cfg)
        daily_history_data = (await self._load_daily_history(saves_dir, source_node, cleanup=True)
                              if "P" in current_layout else None)

        # Waypoint ordering: dump WaypointList (in-memory only) via hot injection
        # when the shared cache is missing or a campaign restart was detected —
        # it's static for a stable campaign.
        sort_zones_by_wp = _bool_cfg(cfg.get("sort_zones_by_waypoint"))
        waypoint_map: dict = {}
        if sort_zones_by_wp:
            wp_cache_file = os.path.join(saves_dir, ".fhc", "fhc_waypoints.lua")
            cache_missing = True
            try:
                await node.read_file(wp_cache_file)
                cache_missing = False
            except Exception:
                cache_missing = True
            if (cache_missing or campaign_restarted_now) and server.status in HOT_STATES:
                try:
                    await hot_write_waypoints(server)
                    await asyncio.sleep(1.5)
                    node.invalidate(wp_cache_file)
                except Exception as e:
                    self.log.warning(f"Fh_Report [{instance_name}]: waypoint hot-write failed: {e}")
            try:
                waypoint_map = await load_waypoint_list(saves_dir, node)
            except Exception as e:
                self.log.warning(f"Fh_Report [{instance_name}]: could not load waypoint cache: {e}")
                waypoint_map = {}

        u2rn = {d.get("ucid"): n for n, d in players.items() if d.get("ucid")}
        embed = build_embed(
            zones, players, cfg,
            report_layout     = current_layout,
            points_detail     = current_detail,
            campaign_stats    = _by_display_name(campaign_by_id, players, names_by_id, u2rn),
            session_stats_raw = _by_display_name(session_by_id, players, names_by_id, u2rn),
            daily_points      = _by_display_name(daily_pts, players, names_by_id, u2rn),
            daily_stats_raw   = _by_display_name(daily_stats, players, names_by_id, u2rn),
            punishment_points = punishment_points,
            daily_history     = daily_history_data,
            waypoint_map      = waypoint_map,
            # Full name -> UCID map for this cycle (every past name each
            # player has had, plus current ones) — lets the Podium identify
            # old entries by UCID and show the player's current name/rank.
            name_to_ucid      = name_to_ucid if "P" in current_layout else None,
            map_name          = map_name,
            bnb               = bnb,
        )

        await self._publish_embed(instance_name, channel, channel_id, cfg, embed)

    async def _post_not_started(self, server, cfg: dict, channel, channel_id,
                                saves_dir: str, node, map_name: str | None = None) -> None:
        """No Foothold save yet (new server / campaign not started): post a
        "not started" embed, with the rank table if Foothold_Ranks.lua exists.
        Logged once (DEBUG) per instance, not every cycle."""
        instance_name = _server_key(server)
        saves_key = _saves_key(server.node, saves_dir)
        self._campaign_absent.add(saves_key)   # the save reappearing marks a new campaign session
        self._campaign_watch.pop(saves_key, None)
        if instance_name not in _missing_save_warned:
            _missing_save_warned.add(instance_name)
            self.log.debug(
                f"Fh_Report [{instance_name}]: no foothold_*.lua found in {saves_dir} — showing "
                f"'campaign not started' until the Foothold mission creates its save files."
            )
        try:
            players = await parse_ranks(os.path.join(saves_dir, "Foothold_Ranks.lua"),
                                        cfg.get("excluded_ucids") or [], node)
        except Exception:
            players = {}
        if players and _HAS_HOOK:
            try:
                players = _fh_hook.post_process(players, cfg, server.instance.name, {})
            except Exception as e:
                self.log.debug(f"Fh_Report [{instance_name}]: fh_hook.post_process failed: {e}")
        punishment_points = {}
        if players and _bool_cfg(cfg.get("show_punishment")):
            if self._cycle_punishment is None:
                self._cycle_punishment = await self._fetch_punishment_points()
            punishment_points = self._cycle_punishment
        layout, detail = self._resolve_report_layout(instance_name, cfg)
        embed = build_not_started_embed(players, cfg, layout, detail, punishment_points, map_name)
        await self._publish_embed(instance_name, channel, channel_id, cfg, embed)

    async def _publish_embed(self, instance_name: str, channel, channel_id, cfg: dict,
                             embed: discord.Embed) -> None:
        """Edit this instance's report message, adopting or posting one if needed."""
        try:
            msg_id = self._message_ids.get(instance_name)
            msg = None
            if msg_id:
                # Edit by id without fetching first: one API call instead of two.
                try:
                    await channel.get_partial_message(msg_id).edit(embed=embed)
                    return
                except discord.NotFound:
                    self.log.warning(f"Fh_Report [{instance_name}]: previous message not found, searching channel for an existing one.")
                    self._message_ids.pop(instance_name, None)

            if msg is None:
                # Unknown message id: adopt an existing Fh_Report message in the channel
                # before posting, so duplicate posts can't happen.
                campaign_name  = cfg.get("campaign_name", "Foothold Campaign")
                expected_title = f"📡  {campaign_name}"
                async for hist_msg in channel.history(limit=50):
                    if (hist_msg.author.id == self.bot.user.id and hist_msg.embeds
                            and (hist_msg.embeds[0].title or "").split("\n")[0] == expected_title):
                        msg = hist_msg
                        self._message_ids[instance_name] = msg.id
                        self._save_message_ids()
                        self.log.info(
                            f"Fh_Report [{instance_name}]: adopted existing message "
                            f"{msg.id} in channel {channel_id} (message_ids.json was out of sync)."
                        )
                        break

            if msg is not None:
                await msg.edit(embed=embed)
                return

            msg = await channel.send(embed=embed)
            self._message_ids[instance_name] = msg.id
            self._save_message_ids()

        except discord.HTTPException as e:
            self.log.error(f"Fh_Report [{instance_name}]: Discord error: {e}")

    # ── /fh_report player ────────────────────────────────────────────────

    # ── Which servers have a block in fh_report.yaml ───────────────────────
    # A block is written either as `<node>: <instance>: {...}` (the layout
    # DCSServerBot recommends on a cluster, where instance names repeat) or as
    # the flat `<instance>: {...}`; both can live in the same file.

    def _block_for(self, server) -> tuple[dict | None, str | None]:
        """(block, "node" | "flat") of this server, or (None, None). The
        `<node>: <instance>` entry wins over the flat one."""
        raw = self.locals or {}
        instance = server.instance.name
        node = _node_name(server)
        node_block = raw.get(node) if node else None
        if isinstance(node_block, dict) and isinstance(node_block.get(instance), dict) and node_block[instance]:
            return node_block[instance], "node"
        flat = raw.get(instance)
        if isinstance(flat, dict) and flat and instance != "DEFAULT" and instance not in self._node_names():
            return flat, "flat"
        return None, None

    def _node_names(self) -> set[str]:
        return {_node_name(s) for s in self.bot.servers.values() if _node_name(s)}

    def _ambiguous_flat(self) -> set[str]:
        """Instance names that several running servers share while only a flat
        block names them: the block cannot say which one it is for."""
        groups: dict[str, int] = {}
        for srv in self.bot.servers.values():
            if srv.status == Status.UNREGISTERED:
                continue
            if self._block_for(srv)[1] == "flat":
                groups[srv.instance.name] = groups.get(srv.instance.name, 0) + 1
        return {name for name, n in groups.items() if n > 1}

    def _has_block(self, server) -> bool:
        block, kind = self._block_for(server)
        if not block:
            return False
        return not (kind == "flat" and server.instance.name in self._ambiguous_flat())

    def _configured_servers(self) -> list:
        """The servers Fh_Report works with: those with a (non-ambiguous) block."""
        ambiguous = self._ambiguous_flat()
        out = []
        for srv in self.bot.servers.values():
            block, kind = self._block_for(srv)
            if block and not (kind == "flat" and srv.instance.name in ambiguous):
                out.append(srv)
        return out

    def _eligible_servers(self, interaction: discord.Interaction) -> list:
        """Configured servers the user can actually pick: the ones DCSServerBot
        has registered (and shows to this user), which is what the `server`
        option lists. A leftover or offline block in fh_report.yaml must not count
        as a second server the user can't see."""
        is_admin = _FHServerTransformer.is_admin(interaction)
        out = []
        for srv in self._configured_servers():
            if srv.status == Status.UNREGISTERED:
                continue
            if (is_admin and srv.locals.get("managed_by") and
                    not utils.check_roles(srv.locals.get("managed_by"), interaction.user)):
                continue
            out.append(srv)
        return out

    def _warn_config_mismatches(self, raw: dict) -> None:
        """Log (once) what in fh_report.yaml cannot work: servers that share an
        instance name without the config saying which node, and blocks that
        match no server. A grace period avoids false alarms while remote nodes
        are still registering; a warning re-arms once the block matches again."""
        for name in sorted(self._ambiguous_flat()):
            if name not in _ambiguous_warned:
                _ambiguous_warned.add(name)
                nodes = ", ".join(sorted(_node_name(s) or "?" for s in self.bot.servers.values()
                                         if s.instance.name == name and s.status != Status.UNREGISTERED))
                self.log.warning(
                    f"Fh_Report: instance '{name}' exists on several nodes ({nodes}) and fh_report.yaml names it "
                    f"without a node, so none of them gets a report. Write the block as '<node>: {name}: ...' "
                    f"(one per node), or give the instances different names.")
        _ambiguous_warned.intersection_update(self._ambiguous_flat())

        nodes = self._node_names()
        servers = list(self.bot.servers.values())
        now_ts = datetime.now(timezone.utc).timestamp()
        entries = []                       # (label, matched, hint)
        for key, value in raw.items():
            if key == "DEFAULT":
                continue
            if key in nodes and isinstance(value, dict):
                for instance in value:
                    entries.append((f"{key}/{instance}",
                                    any(_node_name(s) == key and s.instance.name == instance for s in servers),
                                    f"node '{key}' has no instance named '{instance}'"))
            else:
                hint = "check the instance name in nodes.yaml"
                if isinstance(value, dict) and value and all(isinstance(v, dict) for v in value.values()):
                    hint = "it looks like a node block, but no node has this name"
                entries.append((key, any(s.instance.name == key for s in servers), hint))
        for label, matched, hint in entries:
            if matched:
                _unmatched_instance_since.pop(label, None)
                _unmatched_instance_warned.discard(label)
                continue
            first_seen = _unmatched_instance_since.setdefault(label, now_ts)
            if (now_ts - first_seen) < _UNMATCHED_INSTANCE_GRACE_SECONDS:
                continue
            if label not in _unmatched_instance_warned:
                _unmatched_instance_warned.add(label)
                self.log.warning(
                    f"Fh_Report: block '{label}' in fh_report.yaml doesn't match any DCSServerBot "
                    f"server — {hint}. This block will be skipped until fixed.")

    def _migrate_message_ids(self) -> None:
        """Message ids used to be keyed by instance name only; they are now keyed
        node/instance. A plain key moves to the one server that has that instance
        name; if several do it is dropped (the plugin finds its message again by
        searching the channel, so nothing is posted twice)."""
        legacy = [k for k in self._message_ids if "/" not in k]
        changed = False
        for key in legacy:
            owners = [s for s in self.bot.servers.values() if s.instance.name == key]
            if not owners:
                continue                          # not registered yet: try again next cycle
            value = self._message_ids.pop(key)
            if len(owners) == 1 and _node_name(owners[0]):
                self._message_ids.setdefault(_server_key(owners[0]), value)
            changed = True
        if changed:
            self._save_message_ids()

    def _channel_server(self, interaction: discord.Interaction):
        """The server DCSServerBot assigns to the channel of the command, or None."""
        try:
            return self.bot.get_server(interaction)
        except Exception:
            return None

    def _server_by_key(self, key: str):
        """The Server for a `node/instance` key (a bare instance name also
        works when only one server has it)."""
        servers = list(self.bot.servers.values())
        for server in servers:
            if _server_key(server) == key:
                return server
        owners = [s for s in servers if s.instance.name == key]
        return owners[0] if len(owners) == 1 else None

    def _merged_cfg(self, who) -> dict:
        """DEFAULT plus the block of a server (a Server, or its key)."""
        server = who if not isinstance(who, str) else self._server_by_key(who)
        raw = self.locals or {}
        cfg = dict(raw.get("DEFAULT") or {})
        if server is not None:
            cfg.update(self._block_for(server)[0] or {})
        elif isinstance(who, str) and isinstance(raw.get(who), dict):
            cfg.update(raw[who])
        return cfg

    def _allowed_command_channels(self, instance_name: str) -> set[str]:
        """The set of channel IDs (as strings) where /fh_report commands may
        be used for this instance: its own report channel_id, plus any
        extra channels listed in commands_channel_id (comma-separated
        string or YAML list — either is accepted)."""
        cfg = self._merged_cfg(instance_name)
        allowed = set()
        own = _single_channel_id(cfg.get("channel_id"))
        if own:
            allowed.add(str(own))
        extra = cfg.get("commands_channel_id")
        if isinstance(extra, str):
            allowed.update(x.strip() for x in extra.split(",") if x.strip())
        elif isinstance(extra, list):
            allowed.update(str(x).strip() for x in extra if str(x).strip())
        elif extra:
            allowed.add(str(extra))
        return allowed

    def _public_server_name(self, instance_name: str) -> str:
        """Public (DCS) server name for a configured instance — what users
        actually know it as. Never falls back to the internal nodes.yaml
        instance name, which users have no reason to recognise."""
        srv = self._server_by_key(instance_name)
        return srv.name if srv else "this server"

    def _resolve_server(self, interaction: discord.Interaction,
                        server_param) -> tuple[str | None, str | None]:
        """Which configured instance a command applies to, plus its channel
        check. Returns (instance_name, error_message), exactly one None.
        server_param may be a Server (transformer), a public server name
        (autocomplete namespace) or None.
        - Instance: with one available (registered in DCSServerBot, so listed in
          the `server` option) it's always that one; with several the `server`
          option is required, except in the channel DCSServerBot assigns to one
          of them (the server the option's list pre-selects). Blocks of servers
          that are not registered don't count: they can't be picked.
        - Channel: unrestricted unless commands_channel_id is set; then only the
          instance's channel_id or one listed in commands_channel_id.
        """
        configured = self._configured_servers()
        if not configured:
            if any(k != "DEFAULT" for k in (self.locals or {})):
                return None, ("❌ That server isn't currently available in DCSServerBot — it may still be "
                              "registering, try again shortly.")
            return None, "❌ Fh_Report has no servers configured."

        # Nothing registered yet (still starting up): fall back to the whole config.
        available = self._eligible_servers(interaction) or configured
        if len(available) == 1:
            srv = available[0]
        else:
            if not server_param:
                # The `server` list pre-selects the server of the channel the command is
                # run in (DCSServerBot's own rule) and lists only that one; leaving the
                # option empty must mean exactly that server, not an error.
                srv = self._channel_server(interaction)
                if srv is None or srv not in available:
                    names = ", ".join(f"**{s.name}**" for s in available)
                    return None, (
                        f"❌ More than one server is available ({names}) — please choose one "
                        f"from the `server` option list."
                    )
            elif isinstance(server_param, str):
                srv = self.bot.servers.get(server_param)
                if srv is None:
                    return None, (
                        f"❌ Unknown server **{server_param}** — please choose one "
                        f"from the `server` option list."
                    )
            else:
                srv = server_param
            if srv not in configured:
                return None, f"❌ Fh_Report isn't configured for **{srv.name}**."
        server_name = _server_key(srv)

        cfg = self._merged_cfg(server_name)
        if cfg.get("commands_channel_id") and str(interaction.channel_id) not in self._allowed_command_channels(server_name):
            return None, (
                f"❌ This command for **{self._public_server_name(server_name)}** can't be "
                f"used in this channel. Run it in the channel where its embed is posted, "
                f"or in one of its configured `commands_channel_id` channels."
            )
        return server_name, None

    def _is_admin(self, interaction: discord.Interaction, server_name: str) -> bool:
        """True if the user has a role or name listed in the comma-separated
        'admin' setting (default 'Admin'; a legacy YAML list also works).
        """
        cfg       = self._merged_cfg(server_name)
        admin_raw = cfg.get("admin") or "Admin"
        if isinstance(admin_raw, list):
            admin_list = [str(x).strip() for x in admin_raw if str(x).strip()]
        else:
            admin_list = [x.strip() for x in str(admin_raw).split(",") if x.strip()]
        user_role_names = [r.name for r in interaction.user.roles] if hasattr(interaction.user, "roles") else []
        user_names = {interaction.user.name, getattr(interaction.user, "display_name", None),
                      getattr(interaction.user, "global_name", None)}
        user_names.discard(None)
        for entry in admin_list:
            if entry in user_role_names or entry in user_names:
                return True
        return False

    async def _command_context(self, interaction: discord.Interaction, server_param):
        """Shared slash-command prologue: defer, resolve the instance (and
        its channel restriction — see _resolve_server), and load its config.
        Returns (instance_name, server, cfg, saves_dir, node, ephemeral), or
        None after already sending the error. `node` caches reads for the
        duration of the command."""
        ephemeral = utils.get_ephemeral(interaction)
        await interaction.response.defer(ephemeral=ephemeral)
        instance_name, err = self._resolve_server(interaction, server_param)
        if err:
            await interaction.followup.send(err, ephemeral=True)
            return None
        srv = self._server_by_key(instance_name)
        if srv is None:
            await interaction.followup.send(
                "❌ That server isn't currently available in DCSServerBot — it may still be registering, try again shortly.",
                ephemeral=True)
            return None
        cfg = self._merged_cfg(instance_name)
        saves_dir = await _resolve_saves_dir(srv, cfg)
        return instance_name, srv, cfg, saves_dir, _UpdateReadCache(srv.node), ephemeral

    async def _autocomplete_report_player(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        # The `server` option (if already filled in) is available via the namespace.
        server_param = getattr(interaction.namespace, "server", None)
        server_name, _ = self._resolve_server(interaction, server_param)
        if not server_name:
            return []
        # Non-admins never get name suggestions — they can only query themselves,
        # which doesn't need the player_name parameter at all.
        if not self._is_admin(interaction, server_name):
            return []

        # DCSServerBot's players table: cheap per keystroke, and returns a UCID so
        # the command matches by UCID, not by name.
        try:
            pattern = f"%{current}%"
            async with self.apool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "SELECT ucid, name FROM players WHERE name ILIKE %s ORDER BY name LIMIT 25",
                        (pattern,)
                    )
                    rows = await cur.fetchall()
            return [
                app_commands.Choice(name=(name or ucid)[:100], value=ucid)
                for ucid, name in rows if ucid
            ]
        except Exception:
            # Fall back to the previous file-based approach rather than
            # leaving the admin with no suggestions at all — covers the
            # case where the `players` table/columns aren't what's expected.
            pass

        srv = self._server_by_key(server_name)
        if srv is None:
            return []
        cfg       = self._merged_cfg(server_name)
        try:
            saves_dir = await _resolve_saves_dir(srv, cfg)
        except Exception:
            return []
        try:
            excluded_ucids = cfg.get("excluded_ucids") or []
            ranks_file     = os.path.join(saves_dir, "Foothold_Ranks.lua")
            players        = await parse_ranks(ranks_file, excluded_ucids, srv.node)
        except Exception:
            return []
        names    = sorted(players.keys())
        filtered = [n for n in names if current.lower() in n.lower()]
        return [app_commands.Choice(name=n, value=n) for n in filtered][:25]

    fh_report = Group(
        name="fh_report",
        description="Read-only Foothold campaign reports.",
        guild_only=True
    )

    @fh_report.command(name="player", description="Show a player's rank, session and career stats (read-only).")
    @app_commands.describe(
        _server="Which server — only needed if more than one is configured.",
        player_name="Player name — admin only. Leave empty to see your own stats."
    )
    @app_commands.rename(_server="server")
    @app_commands.autocomplete(player_name=_autocomplete_report_player)
    async def player(self, interaction: discord.Interaction,
                     _server: app_commands.Transform[Server, _FHServerTransformer] | None = None,
                     player_name: str | None = None):
        # Named `_server` (shown as "server" via rename): DCSServerBot core
        # auto-fills a parameter literally named `server` from the channel,
        # bypassing our transformer (per Special K).
        ctx = await self._command_context(interaction, _server)
        if ctx is None:
            return
        server, srv, cfg, saves_dir, node, ephemeral = ctx

        if player_name and not self._is_admin(interaction, server):
            await interaction.followup.send(
                "❌ You can only view your own stats. Leave the `player_name` field empty.",
                ephemeral=True)
            return

        try:
            # Never force bc:saveToDisk() here (per Leka): Foothold autosaves every
            # 60s and a save is a heavy ~10,000-line write. Read the latest autosave.
            persistence_file = await find_persistence_file(saves_dir, node)
            if not persistence_file:
                await interaction.followup.send(
                    f"❌ No Foothold save file found for **{self._public_server_name(server)}**.", ephemeral=True)
                return
            ranks_file = os.path.join(saves_dir, "Foothold_Ranks.lua")

            excluded_ucids = cfg.get("excluded_ucids") or []
            try:
                players    = await parse_ranks(ranks_file, excluded_ucids, node)
            except FileNotFoundError:
                await interaction.followup.send(
                    f"❌ No campaign rankings yet for **{self._public_server_name(server)}** — "
                    f"fly a mission first, then try again.", ephemeral=True)
                return
            campaign_stats, session_stats_raw, name_to_ucid_native = await parse_player_stats(persistence_file, node)
        except Exception as e:
            await interaction.followup.send(f"❌ Error reading campaign files:\n```{e}```", ephemeral=True)
            return

        last_seen = None
        if player_name:
            # Admin path. An autocomplete pick is a UCID: match by UCID, immune to
            # callsign/name formatting differences.
            match = None
            if re.fullmatch(r"[0-9a-f]{32}", player_name.lower()):
                target_ucid = player_name.lower()
                match = next((n for n, d in players.items() if d.get("ucid") == target_ucid), None)
                if match is None:
                    await interaction.followup.send(
                        f"❌ No campaign stats found for that player on **{self._public_server_name(server)}** yet.",
                        ephemeral=True)
                    return
            if match is None:
                # Free-typed text (autocomplete not used, or no UCID match) —
                # fall back to matching by name as before.
                match = next((n for n in players if n.lower() == player_name.lower()), None)
                if match is None:
                    match = next(
                        (n for n in players if strip_callsign(n).lower() == strip_callsign(player_name).lower()),
                        None
                    )
                if match is None:
                    await interaction.followup.send(
                        f"❌ Player **{_safe_code_span(player_name)}** not found in **{self._public_server_name(server)}**.\n"
                        f"Check the exact name (case-sensitive autocomplete is available).",
                        ephemeral=True)
                    return
        else:
            # Self-lookup path — resolve the caller's own UCID via DCSSB's
            # players table (same linking used by /linkme), then match it
            # against the parsed Foothold roster.
            own_ucid = None
            try:
                async with self.apool.connection() as conn:
                    async with conn.cursor() as cur:
                        # UCID and last_seen in one round trip.
                        await cur.execute(
                            "SELECT p.ucid, (SELECT MAX(s.hop_off) FROM statistics s "
                            "WHERE s.player_ucid = p.ucid) "
                            "FROM players p WHERE p.discord_id = %s LIMIT 1",
                            (interaction.user.id,)
                        )
                        row = await cur.fetchone()
                        own_ucid = row[0] if row else None
                        last_seen = row[1] if row else None
            except Exception as e:
                await interaction.followup.send(f"❌ Error looking up your account:\n```{e}```", ephemeral=True)
                return
            if not own_ucid:
                await interaction.followup.send(
                    "❌ Your Discord account isn't linked to a DCS UCID yet. Use `/linkme` first.",
                    ephemeral=True)
                return
            match = next((n for n, d in players.items() if d.get("ucid") == own_ucid), None)
            if match is None:
                await interaction.followup.send(
                    f"❌ No campaign stats found for you on **{self._public_server_name(server)}** yet — "
                    f"fly a mission first, then try again.",
                    ephemeral=True)
                return

        data = players[match]

        # Same UCID-first identity as the embed; the stripped-name lookup is
        # only a fallback for UCID-less saves.
        daily_snap = await self._load_daily_snapshot(saves_dir, node)
        snap_unpacked = _unpack_daily_snapshot(daily_snap)
        campaign_by_id, session_by_id, names_by_id, name_to_ucid = self._identify_players(
            snap_unpacked, campaign_stats, session_stats_raw, players, name_to_ucid_native)
        u2rn     = {d.get("ucid"): n for n, d in players.items() if d.get("ucid")}
        cs_disp  = _by_display_name(campaign_by_id, players, names_by_id, u2rn)
        srs_disp = _by_display_name(session_by_id, players, names_by_id, u2rn)

        match_base = strip_callsign(match)

        def _lookup(by_name: dict, default):
            if match in by_name:
                return by_name[match]
            k = next((k for k in by_name if strip_callsign(k) == match_base), None)
            return by_name[k] if k is not None else default

        s_pts   = _lookup(cs_disp, 0)
        s_stats = _lookup(srs_disp, None) or {}

        # Daily points — reuse the same snapshot-based computation as the embed
        tz = self._reset_tz(srv)
        reset_hour = _reset_time_today(cfg, tz)
        daily_pts_all, daily_stats_all, _ = await self._compute_daily_points(
            saves_dir, campaign_by_id, session_by_id, reset_hour, node,
            os.path.basename(persistence_file) if persistence_file else None,
            name_to_ucid, names_by_id, set(campaign_stats) | set(session_stats_raw), daily_snap,
            persist=False, snap_unpacked=snap_unpacked, tz=tz)
        d_pts   = _lookup(_by_display_name(daily_pts_all, players, names_by_id, u2rn), 0)
        d_stats = _lookup(_by_display_name(daily_stats_all, players, names_by_id, u2rn), None) or {}

        # UCID + last_seen from DCSServerBot core tables
        ucid = data.get("ucid")
        if ucid and not last_seen:
            try:
                async with self.apool.connection() as conn:
                    async with conn.cursor() as cur:
                        await cur.execute(
                            "SELECT MAX(hop_off) FROM statistics WHERE player_ucid = %s",
                            (ucid,)
                        )
                        row = await cur.fetchone()
                        last_seen = row[0] if row else None
            except Exception:
                pass

        # Mission status — read-only wording (no "changes applied")
        if srv.status == Status.RUNNING:
            mission_status = f"🟢 **{self._public_server_name(server)}** Mission running."
        elif srv.status == Status.PAUSED:
            mission_status = f"⏸️ **{self._public_server_name(server)}** Mission paused."
        else:
            mission_status = f"⏹️ **{self._public_server_name(server)}** Mission not running."

        sess = await _read_fhr_json(node, saves_dir, "session.json")
        bnb, penalties = (await self._fetch_bnb_detail(
            srv, cfg, _session_start_for(sess, cfg, tz), ucid,
            with_penalties=_bool_cfg(cfg.get("show_punishment")), with_bnb=_show_bob(cfg))
            if ucid else (None, None))
        embed = _build_player_report_embed(
            player_name=match, data=data, ucid=ucid, last_seen=last_seen,
            session_points=s_pts, daily_points=d_pts, session_stats=s_stats,
            mission_status=mission_status, daily_stats=d_stats,
            bnb=bnb, penalties=penalties,
        )
        await interaction.followup.send(embed=embed, ephemeral=ephemeral)

    async def _reply_temporarily(self, interaction: discord.Interaction, text: str, ok: bool = False) -> None:
        """Private reply that removes itself: after 5 s when it reports success,
        after 30 s for anything else (errors, refusals), so the channel stays clean."""
        message = await interaction.followup.send(text, ephemeral=True)
        if message is None or not hasattr(message, "delete"):
            return

        async def remove():
            await asyncio.sleep(5 if ok else 30)
            try:
                await message.delete()
            except Exception:
                pass            # already dismissed, or the interaction token expired
        task = asyncio.get_running_loop().create_task(remove())
        tasks = self.__dict__.setdefault("_temp_replies", set())
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    @fh_report.command(name="update", description="Admin only: refresh the report embed now, even if the mission is paused or stopped.")
    @app_commands.describe(_server="Which server — only needed if more than one is configured.")
    @app_commands.rename(_server="server")
    async def update(self, interaction: discord.Interaction,
                     _server: app_commands.Transform[Server, _FHServerTransformer] | None = None):
        # See player()'s comment on why this is `_server` + rename.
        await interaction.response.defer(ephemeral=True)
        key, err = self._resolve_server(interaction, _server)
        if err:
            await self._reply_temporarily(interaction, err)
            return
        srv = self._server_by_key(key)
        if srv is None:
            await self._reply_temporarily(interaction, 
                "❌ That server isn't currently available in DCSServerBot — it may still be registering, try again shortly.")
            return
        if not self._is_admin(interaction, key):
            await self._reply_temporarily(interaction, "❌ Only admins can refresh the report.")
            return
        cfg = self._merged_cfg(key)
        name = self._public_server_name(key)
        if not _updates_enabled(cfg):
            await self._reply_temporarily(interaction, 
                f"❌ The report of **{name}** is switched off (`enable_updates: false`).")
            return
        channel_id = _single_channel_id(cfg.get("channel_id"))
        channel = self.bot.get_channel(int(channel_id)) if channel_id else None
        if channel is None:
            await self._reply_temporarily(interaction, 
                f"❌ **{name}** has no usable report channel (`channel_id` missing or not found).")
            return
        self._cycle_punishment = None        # fresh penalties, not the last cycle's
        try:
            await self._update_server(srv, cfg)
        except Exception as e:
            self.log.error(f"Fh_Report [{key}]: manual update failed: {e}", exc_info=True)
            await self._reply_temporarily(interaction, f"❌ The update failed:\n```{e}```")
            return
        finally:
            self._cycle_punishment = None
        mid = self._message_ids.get(key)
        link = f" {channel.get_partial_message(mid).jump_url}" if mid else ""
        await self._reply_temporarily(interaction, f"✅ Report of **{name}** updated.{link}", ok=True)

    @fh_report.command(name="podium", description="Show who held a given daily-history position between two dates.")
    @app_commands.describe(
        date_from="Start date (YYYY-MM-DD)",
        date_to="End date (YYYY-MM-DD)",
        top="Show the top N positions for each day (1-50)",
        _server="Which server — only needed if more than one is configured."
    )
    @app_commands.rename(_server="server")
    async def podium(self, interaction: discord.Interaction,
                     date_from: str, date_to: str, top: app_commands.Range[int, 1, 50],
                     _server: app_commands.Transform[Server, _FHServerTransformer] | None = None):
        # See player()'s comment above on why this is `_server` + rename,
        # not a plain `server` parameter.
        ctx = await self._command_context(interaction, _server)
        if ctx is None:
            return
        server, srv, cfg, saves_dir, node, ephemeral = ctx

        try:
            d_from = datetime.strptime(date_from.strip(), "%Y-%m-%d").date()
            d_to   = datetime.strptime(date_to.strip(), "%Y-%m-%d").date()
        except ValueError:
            await interaction.followup.send(
                "❌ Invalid date format — use `YYYY-MM-DD` for both `date_from` and `date_to` "
                "(e.g. `2026-08-01`).", ephemeral=True)
            return
        if d_from > d_to:
            await interaction.followup.send(
                "❌ `date_from` must not be after `date_to`.", ephemeral=True)
            return

        try:
            history = await self._load_daily_history(saves_dir, node)
        except Exception as e:
            await interaction.followup.send(f"❌ Error reading daily history:\n```{e}```", ephemeral=True)
            return

        # Filter to the requested [date_from, date_to] range (inclusive)
        filtered_history = {}
        for date_str, events in history.items():
            try:
                d = datetime.strptime(date_str, "%Y-%m-%d").date()
            except ValueError:
                continue
            if d_from <= d <= d_to:
                filtered_history[date_str] = events

        if not filtered_history:
            await interaction.followup.send(
                f"No history found for **{self._public_server_name(server)}** between `{date_from}` and `{date_to}`.",
                ephemeral=True)
            return

        # Need the current roster for live rank lookups, same as the main embed
        try:
            excluded_ucids = cfg.get("excluded_ucids") or []
            ranks_file     = os.path.join(saves_dir, "Foothold_Ranks.lua")
            players        = await parse_ranks(ranks_file, excluded_ucids, node)
        except Exception:
            players = {}

        podium_lines = _build_podium_table(
            filtered_history, players, days=0, top=top,
            strip_callsign_flag=_bool_cfg(cfg.get("strip_callsign")),
            name_to_ucid=_unpack_daily_snapshot(await self._load_daily_snapshot(saves_dir, node)).get("name_to_ucid") or {}
        )
        if not podium_lines:
            await interaction.followup.send(
                f"No history found with data for the top **{top}** position(s) between "
                f"`{date_from}` and `{date_to}`.", ephemeral=True)
            return

        embed = discord.Embed(
            title=f"👑 Daily Podium — Top {top}",
            description=f"{date_from} → {date_to}",
            color=0xF1C40F, timestamp=datetime.now(timezone.utc)
        )
        _add_podium_field(embed, "👑", podium_lines)
        embed.set_footer(text=f"{_version_text()} · Read-only historical report")
        await interaction.followup.send(embed=embed, ephemeral=ephemeral)


async def setup(bot: DCSServerBot):
    await bot.add_cog(Fh_Report(bot))
