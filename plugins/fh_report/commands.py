"""
FH_Report Plugin for DCSServerBot
Reads Foothold campaign save files and posts/updates a Discord embed
with front-line status and pilot leaderboard. No database required.
"""

from __future__ import annotations

import glob
import json
import logging
import os
import re
import asyncio
from datetime import datetime, timezone, timedelta
from typing import Type

import discord
from discord import app_commands
from discord.ext import tasks
from core import Plugin, TEventListener, utils, Status, Group
from services.bot import DCSServerBot


from .version import __version__

log = logging.getLogger(__name__)

# Shown in every embed footer — bumped manually alongside each GitHub
# release, independent of version.py (which DCSSB manages/reads on its own
# terms; keeping this separate avoids the conflicts that caused).
FH_REPORT_RELEASE = "12.0.0"

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


class _UpdateReadCache:
    """Reuse remote file reads within one server's current update only.

    Several parse steps in one update cycle (parse_zones, parse_ranks,
    parse_player_stats) read the same persistence file independently. This
    wrapper memoizes read_file() results for the lifetime of a single
    _update_server() call so the same path is only fetched once from the
    node, cutting down redundant I/O — especially relevant when the node is
    remote. It intentionally does NOT persist across update cycles, so it
    can never serve data older than the current cycle. Call invalidate()
    right after writing a file this cache may have already read, so a
    later read in the same cycle doesn't return the stale pre-write copy.
    """

    def __init__(self, node):
        self._node = node
        self._files: dict[str, bytes] = {}

    async def read_file(self, path: str) -> bytes:
        if path not in self._files:
            self._files[path] = await self._node.read_file(path)
        return self._files[path]

    async def list_directory(self, path: str):
        return await self._node.list_directory(path)

    def invalidate(self, path: str) -> None:
        self._files.pop(path, None)


def _validated_update_interval(raw: dict, default: int = 300) -> tuple[int, str | None]:
    """Validate DEFAULT.update_interval from fh_report.yaml.

    A misconfigured value of zero, negative, or non-numeric would otherwise
    make the periodic updater loop run flat-out (or crash outright), so this
    falls back to `default` and returns a warning message for the caller to
    log — rather than either silently misbehaving or refusing to load the
    whole plugin over one bad config value.
    """
    configured = (raw.get("DEFAULT") or {}).get("update_interval", default)
    try:
        interval = int(configured)
    except (TypeError, ValueError):
        return default, (
            f"FH_Report: DEFAULT.update_interval ({configured!r}) is not a valid "
            f"integer — falling back to the default of {default}s."
        )
    if interval <= 0:
        return default, (
            f"FH_Report: DEFAULT.update_interval ({interval}) must be greater than "
            f"zero — falling back to the default of {default}s."
        )
    return interval, None


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
    """Returns (display_total, display_active) — how many of the 5 drawable
    slot symbols to show, and how many of those should render as active.

    With 5 or fewer real slots (total_slots), this is a direct 1:1 count,
    same as before. With more than 5 real slots, the 5 symbols instead
    represent a PROPORTION of real progress: display_active =
    round((active_slots / total_slots) * 5), using standard round-half-up
    (0.5 always rounds up, not Python's built-in banker's rounding which
    would round 2.5 down to 2). This avoids a zone with, say, 6 of 9 slots
    active looking visually identical to one with 9 of 9 (both would
    otherwise show as "5 filled") — the 5 symbols now reflect real progress
    proportionally instead of just being silently capped."""
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


async def find_persistence_file(saves_dir: str, node) -> str | None:
    """Read the active Foothold persistence file path from foothold.status.
    Falls back to most recently modified foothold_*.lua if status file not found.
    Uses server.node.read_file() / list_directory() to support remote nodes in
    a DCSSB cluster (Master reads files from agent disks transparently)."""
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
            # Covers FileNotFoundError (file genuinely missing) and
            # TimeoutError (remote agent-node RPC read timed out — reported
            # by Special K; TimeoutError is a subclass of OSError in Python,
            # same as FileNotFoundError, so this one except now catches
            # both uniformly). Either way, fall through to the
            # list_directory fallback below rather than crashing the whole
            # update cycle with an unhandled exception.
            pass
    except OSError:
        pass
    # Fallback: list directory and find foothold_*.lua candidates
    try:
        entries = await node.list_directory(saves_dir)
        candidates = [
            os.path.join(saves_dir, e) for e in entries
            if e.lower().startswith("foothold_") and e.lower().endswith(".lua")
            and "rank" not in e.lower()
        ]
        if not candidates:
            return None
        return sorted(candidates)[-1]
    except Exception:
        return None


async def parse_zones(filepath: str, node) -> dict:
    """Parse zone persistence file. Returns {'blue': [...], 'red': [...]}."""
    data = await node.read_file(filepath)
    content = data.decode("utf-8")

    zones = {"blue": [], "red": [], "neutral": 0}
    zone_names = [
        a or b for a, b in
        re.findall(
            r'zonePersistance\[["\']zones["\']\]\[(?:"([^"]+)"|\x27([^\x27]+)\x27)\]',
            content
        )
    ]

    # BLUE-only extra-slot mechanic (mirrors ZoneCommander:addExtraSlot):
    # every blue zone can get +1 extra slot unlocked per-zone, and if this
    # mission-wide flag is also true, a SECOND extra slot becomes available
    # too (max = 1 + (globalExtraUnlock and 1 or 0), from zoneCommander.lua).
    # RED has its own separate mechanic (left untouched here) — this is
    # deliberately blue-specific, not a generic per-side computation.
    gxu_m = re.search(r'globalExtraUnlock"?\]\s*=\s*(true|false)', content)
    global_extra_unlock = (gxu_m.group(1) == "true") if gxu_m else False

    for zone in zone_names:
        ez = re.escape(zone)
        sq = chr(39)
        dq = chr(34)
        pattern = (
            rf"zonePersistance\[[{dq}\{sq}]zones[{dq}\{sq}]\]"
            + rf"\[(?:{dq}{ez}{dq}|{sq}{ez}{sq})\] = \{{"
            + r"(.*?)(?=\nzonePersistance|\Z)"
        )
        match = re.search(pattern, content, re.DOTALL)
        if not match:
            continue
        block = match.group(1)

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
        if level > 0:
            # Find remainingUnits block start
            ru_key = '"remainingUnits"' if '"remainingUnits"' in block else "'remainingUnits'"
            ru_start = block.find(f'[{ru_key}]={{')
            if ru_start == -1:
                ru_start = block.find("[" + ru_key + "]={")
            if ru_start != -1:
                # Extract remainingUnits block using brace counting
                bs = block.find('{', ru_start)
                depth, j = 1, bs + 1
                while j < len(block) and depth > 0:
                    if block[j] == '{': depth += 1
                    elif block[j] == '}': depth -= 1
                    j += 1
                ru_block = block[bs + 1:j - 1]
                # Count active slots across ALL slots (1..level), not just the
                # first 5. This ensures zones with active slots beyond position 5
                # (e.g. a base with damaged early slots but live late slots) are
                # correctly shown as still having active defenses.
                # Display is capped at 5 symbols — showing "how many remain active"
                # up to that cap, prioritizing active slots over slot position.
                for idx in range(1, level + 1):
                    # Find [idx]={ using brace counting
                    slot_key = f'[{idx}]={{'
                    sk = ru_block.find(slot_key)
                    if sk == -1:
                        continue
                    sb = sk + len(slot_key) - 1
                    sd, sj = 1, sb + 1
                    while sj < len(ru_block) and sd > 0:
                        if ru_block[sj] == '{': sd += 1
                        elif ru_block[sj] == '}': sd -= 1
                        sj += 1
                    slot_content = ru_block[sb + 1:sj - 1]
                    # Active if any quoted non-empty string inside
                    if re.search(r'["\x27][^"\x27]{1,}["\x27]', slot_content):
                        active_slots += 1

        info = {"name": zone, "level": level, "active_slots": active_slots, "suspended": suspended}
        if side == 2:
            # BLUE true max = base slots (randomUpgradesBlue's own entry
            # count for THIS zone) + the extra-slot allowance above. This
            # can exceed `level` when a slot has been unlocked/purchased
            # but hasn't finished building yet, or simply hasn't been
            # started — those should still show as an empty (not-yet-filled)
            # symbol rather than not being drawn at all.
            ru_key2 = '"randomUpgradesBlue"' if '"randomUpgradesBlue"' in block else "'randomUpgradesBlue'"
            rub_start = block.find(f'[{ru_key2}]={{')
            base_slots = 0
            if rub_start != -1:
                bs2 = block.find('{', rub_start)
                depth2, j2 = 1, bs2 + 1
                while j2 < len(block) and depth2 > 0:
                    if block[j2] == '{': depth2 += 1
                    elif block[j2] == '}': depth2 -= 1
                    j2 += 1
                rub_block = block[bs2 + 1:j2 - 1]
                base_slots = len(re.findall(r'\[\d+\]=', rub_block))
            extra_allowance = 2 if global_extra_unlock else 1
            info["true_max"] = base_slots + extra_allowance
            zones["blue"].append(info)
        elif side == 1:
            zones["red"].append(info)

    return zones


async def parse_player_stats(filepath: str, node) -> tuple[dict, dict, dict]:
    """Parse playerStats from Foothold persistence file.
    Returns (campaign_stats, session_stats_raw, name_to_ucid):
      campaign_stats    = {player_name: points}  (unchanged contract)
      session_stats_raw = {player_name: {stat_key: value}} — used for the
                           session card (show_session_card). Excludes
                           Points/Points spent.
      name_to_ucid      = {player_name: ucid}

    Foothold 4.9.1+ restructured playerStats to be keyed by UCID directly
    (with "name" and a nested "stats" sub-table inside each block),
    replacing the older name-keyed structure with stats stored inline.
    zonePersistance["playerStatsIdentityVersion"] tells us which is
    present:
      nil -> old format (name-keyed, stats inline, no UCID info at all in
             this file — name_to_ucid comes back empty, caller falls back
             to cross-referencing Foothold_Ranks.lua)
      1   -> new format (UCID-keyed; name_to_ucid is built for free from
             the block's own keys, no separate lookup table needed)
      anything else -> a future format not understood yet; skip this file
             and warn once rather than risk misparsing it.
    Same one-time-migration guarantee as parse_ranks (see its docstring) —
    no migration logic needed on our side, just per-read format detection."""
    try:
        data = await node.read_file(filepath)
        content = data.decode("utf-8")

        version_m = re.search(r'zonePersistance\[["\']playerStatsIdentityVersion["\']\]\s*=\s*(\d+)', content)
        version = int(version_m.group(1)) if version_m else None

        if version is not None and version != 1:
            if filepath not in _unsupported_version_warned:
                _unsupported_version_warned.add(filepath)
                logging.getLogger(__name__).warning(
                    f"FH_Report: {filepath} reports playerStatsIdentityVersion={version}, "
                    f"which this version of FH_Report doesn't understand yet — "
                    f"please update FH_Report. Skipping this file for now."
                )
            return {}, {}, {}

        stats_match = re.search(
            r"zonePersistance\[[\"']playerStats[\"']\]\s*=\s*\{",
            content
        )
        if not stats_match:
            return {}, {}, {}
        # Use brace counting to extract the full playerStats block robustly,
        # regardless of inconsistent indentation in the Lua file.
        start  = stats_match.end()
        depth  = 1
        pos    = start
        while pos < len(content) and depth > 0:
            if content[pos] == "{":
                depth += 1
            elif content[pos] == "}":
                depth -= 1
            pos += 1
        block   = content[start:pos - 1]
        results = {}
        raw_all = {}
        name_to_ucid: dict = {}

        if version is None:
            # ── Old format: name-keyed, stats inline in the player block ──
            for m in re.finditer(r"\[[\"']([^\"']+)[\"']\]\s*=\s*\{", block):
                name      = m.group(1)
                blk_start = m.end()
                d = 1
                i = blk_start
                while i < len(block) and d > 0:
                    if block[i] == "{":
                        d += 1
                    elif block[i] == "}":
                        d -= 1
                    i += 1
                player_block = block[blk_start:i - 1]
                pts_m = re.search(r'\[(?:"Points"|\'Points\')\]\s*=\s*(\d+)', player_block)
                if not pts_m:
                    continue
                results[name] = int(pts_m.group(1))
                raw_stats = {}
                for sm in re.finditer(r'\[["\']([^"\']+)["\']\]\s*=\s*(-?\d+(?:\.\d+)?)', player_block):
                    key, val = sm.group(1), sm.group(2)
                    if key == "Points":
                        continue
                    raw_stats[key] = float(val) if "." in val else int(val)
                raw_all[name] = raw_stats

            # Native ucidToName may still be present as a supplementary
            # table even in an old-format file (an interim Foothold step) —
            # use it opportunistically if so, costs nothing if absent.
            ucid_match = re.search(
                r"zonePersistance\[[\"']ucidToName[\"']\]\s*=\s*\{",
                content
            )
            if ucid_match:
                u_start = ucid_match.end()
                u_depth = 1
                u_pos   = u_start
                while u_pos < len(content) and u_depth > 0:
                    if content[u_pos] == "{":
                        u_depth += 1
                    elif content[u_pos] == "}":
                        u_depth -= 1
                    u_pos += 1
                ucid_block = content[u_start:u_pos - 1]
                for um in re.finditer(r"\[[\'\"]([a-f0-9]{32})[\'\"]\]\s*=\s*[\'\"]([^\'\"]+)[\'\"]", ucid_block):
                    name_to_ucid[um.group(2)] = um.group(1)

        else:
            # ── New format (4.9.1+): UCID-keyed, name + nested "stats" ────
            for m in re.finditer(r"\[[\"']([a-f0-9]{32})[\"']\]\s*=\s*\{", block):
                ucid      = m.group(1)
                blk_start = m.end()
                d = 1
                i = blk_start
                while i < len(block) and d > 0:
                    if block[i] == "{":
                        d += 1
                    elif block[i] == "}":
                        d -= 1
                    i += 1
                player_block = block[blk_start:i - 1]

                name_m = re.search(r'\[(?:"name"|\'name\')\]\s*=\s*["\']([^"\']+)["\']', player_block)
                if not name_m:
                    continue
                name = name_m.group(1)

                stats_m = re.search(r'\[(?:"stats"|\'stats\')\]\s*=\s*\{', player_block)
                if not stats_m:
                    continue
                sb = player_block.find('{', stats_m.end() - 1)
                sd, sj = 1, sb + 1
                while sj < len(player_block) and sd > 0:
                    if player_block[sj] == '{': sd += 1
                    elif player_block[sj] == '}': sd -= 1
                    sj += 1
                stats_block = player_block[sb + 1:sj - 1]

                pts_m = re.search(r'\[(?:"Points"|\'Points\')\]\s*=\s*(\d+)', stats_block)
                if not pts_m:
                    continue
                results[name] = int(pts_m.group(1))
                raw_stats = {}
                for sm in re.finditer(r'\[["\']([^"\']+)["\']\]\s*=\s*(-?\d+(?:\.\d+)?)', stats_block):
                    key, val = sm.group(1), sm.group(2)
                    if key == "Points":
                        continue
                    raw_stats[key] = float(val) if "." in val else int(val)
                raw_all[name] = raw_stats
                name_to_ucid[name] = ucid

        return results, raw_all, name_to_ucid
    except Exception:
        return {}, {}, {}


async def hot_write_waypoints(server) -> None:
    """Inject Lua that dumps the mission's in-memory WaypointList table
    (zone name -> waypoint number suffix, set from the .miz's trigger zone
    flavorText at mission load — never persisted to any Foothold save file)
    to saves_dir/.fhc/fhc_waypoints.lua. Same technique and same shared file
    as FH_Control's _hot_write_waypoints, so both plugins benefit from
    whichever one triggers it first on a given server. No-op if WaypointList
    isn't defined in the mission (not every Foothold map sets it up)."""
    lua = (
        "if WaypointList and lfs and io then "
        "  lfs.mkdir(lfs.writedir() .. [[Missions/Saves/.fhc]]) "
        "  local _p = lfs.writedir() .. [[Missions/Saves/.fhc/fhc_waypoints.lua]] "
        "  local _f = io.open(_p, 'w') "
        "  if _f then "
        "    _f:write([[-- FH_Report/FH_Control waypoint cache\n]]) "
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
    """Read fhc_waypoints.lua (written by hot_write_waypoints, possibly by
    FH_Control instead of us — same shared file). Returns {zone_name: wp_number}
    with the numeric part already extracted from the raw suffix string
    (e.g. "3" or "WP3" -> 3). Zones with a non-numeric or missing suffix are
    omitted from the returned dict entirely — callers treat 'not in dict' as
    'no waypoint assigned'. Returns {} if the file doesn't exist or fails to
    parse, which is a normal/expected state (mission never dumped it yet)."""
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


_unsupported_version_warned: set[str] = set()
_unmatched_instance_warned: set[str] = set()
_unmatched_instance_since: dict[str, float] = {}
_UNMATCHED_INSTANCE_GRACE_SECONDS = 120  # tolerate remote-node startup races


async def parse_ranks(filepath: str, excluded_ucids: list[str], node) -> dict:
    """Parse Foothold_Ranks.lua. Returns pilot dict sorted by credits desc,
    keyed by name (unchanged contract) regardless of which on-disk format
    was used. Pilots whose UCID is in excluded_ucids are omitted.

    Foothold 4.9.1+ restructured this file to key RankSave["players"] by
    UCID directly (with "name" nested inside each block), replacing the
    older name-keyed structure + separate RankSave["ucidToName"] lookup
    table. Confirmed with Leka: RankSave["playerIdentityVersion"] (NOT the
    unrelated, pre-existing RankSave["version"], which tracks career-rank
    data separately) tells us which structure is present:
      nil       -> old format (name-keyed, needs ucidToName to resolve UCID)
      1 or 2    -> new format (UCID-keyed; name/credits/career live inside
                   each UCID's own block — no ucidToName needed at all)
      anything else -> a future format we don't understand yet; skip this
                   file entirely rather than risk misparsing it, and warn
                   once that FH_Report needs updating.
    Foothold itself migrates an old-format file to the new one exactly
    once, in memory, the first time it loads with an updated Foothold —
    every subsequent save is internally consistent for whichever version
    it reports, so a per-read version check here is sufficient; FH_Report
    never needs to perform or track any migration of its own."""
    data = await node.read_file(filepath)
    content = data.decode("utf-8")

    version_m = re.search(r'RankSave\[["\']playerIdentityVersion["\']\]\s*=\s*(\d+)', content)
    version = int(version_m.group(1)) if version_m else None

    if version is not None and version not in (1, 2):
        if filepath not in _unsupported_version_warned:
            _unsupported_version_warned.add(filepath)
            logging.getLogger(__name__).warning(
                f"FH_Report: {filepath} reports playerIdentityVersion={version}, "
                f"which this version of FH_Report doesn't understand yet — "
                f"please update FH_Report. Skipping this file for now."
            )
        return {}

    players = {}

    if version is None:
        # ── Old format: name-keyed, separate ucidToName lookup ────────────
        excluded_names: set[str] = set()
        for ucid in excluded_ucids:
            m = re.search(rf"\['{re.escape(ucid)}'\]=\"([^\"]+)\"", content)
            if m:
                excluded_names.add(m.group(1))

        name_to_ucid = {}
        ucid_pattern = r"\[[\'\"]([a-f0-9]{32})[\'\"]\]=[\'\"]([^\'\"]+)[\'\"]"
        for ucid_m in re.finditer(ucid_pattern, content):
            name_to_ucid[ucid_m.group(2)] = ucid_m.group(1)

        players_start = re.search(r"RankSave\[[\"']players[\"']\]\s*=\s*\{", content)
        if not players_start:
            return {}
        bs = content.find('{', players_start.end() - 1)
        depth, j = 1, bs + 1
        while j < len(content) and depth > 0:
            if content[j] == '{': depth += 1
            elif content[j] == '}': depth -= 1
            j += 1
        players_block = content[bs + 1:j - 1]

        pos = 0
        while pos < len(players_block):
            km = re.search(r'\[["\']([^"\']+)["\']\]=\{', players_block[pos:])
            if not km:
                break
            name = km.group(1)
            brace_pos = pos + km.end() - 1
            depth2, k = 1, brace_pos + 1
            while k < len(players_block) and depth2 > 0:
                if players_block[k] == '{': depth2 += 1
                elif players_block[k] == '}': depth2 -= 1
                k += 1
            block = players_block[brace_pos + 1:k - 1]
            pos = pos + km.start() + 1

            credit_m = re.search(r'\[(?:"credits"|\'credits\')\]\s*=\s*([\d.]+)', block)
            if not credit_m:
                continue
            clean_name = name.strip()
            if not clean_name or len(clean_name) < 2:
                continue
            if clean_name in excluded_names:
                continue

            career: dict = {}
            career_m = re.search(r'\[(?:"career"|\'career\')\]\s*=\s*\{', block)
            if career_m:
                cb = block.find('{', career_m.end() - 1)
                cd, cj = 1, cb + 1
                while cj < len(block) and cd > 0:
                    if block[cj] == '{': cd += 1
                    elif block[cj] == '}': cd -= 1
                    cj += 1
                career_block = block[cb + 1:cj - 1]
                for cm in re.finditer(r'\[(\d+)\]\s*=\s*([\d.]+)', career_block):
                    career[int(cm.group(1))] = float(cm.group(2))

            players[clean_name] = {
                "credits": float(credit_m.group(1)),
                "ucid":    name_to_ucid.get(clean_name),
                "career":  career,
            }

    else:
        # ── New format (4.9.1+): UCID-keyed, name/credits/career inline ───
        # No ucidToName lookup needed at all — the UCID is already the
        # block's own key, and "name" lives inside the same block.
        players_start = re.search(r"RankSave\[[\"']players[\"']\]\s*=\s*\{", content)
        if not players_start:
            return {}
        bs = content.find('{', players_start.end() - 1)
        depth, j = 1, bs + 1
        while j < len(content) and depth > 0:
            if content[j] == '{': depth += 1
            elif content[j] == '}': depth -= 1
            j += 1
        players_block = content[bs + 1:j - 1]

        pos = 0
        while pos < len(players_block):
            km = re.search(r'\[["\']([a-f0-9]{32})["\']\]=\{', players_block[pos:])
            if not km:
                break
            ucid = km.group(1)
            brace_pos = pos + km.end() - 1
            depth2, k = 1, brace_pos + 1
            while k < len(players_block) and depth2 > 0:
                if players_block[k] == '{': depth2 += 1
                elif players_block[k] == '}': depth2 -= 1
                k += 1
            block = players_block[brace_pos + 1:k - 1]
            pos = pos + km.start() + 1

            if ucid in excluded_ucids:
                continue

            credit_m = re.search(r'\[(?:"credits"|\'credits\')\]\s*=\s*([\d.]+)', block)
            if not credit_m:
                continue
            name_m = re.search(r'\[(?:"name"|\'name\')\]\s*=\s*["\']([^"\']+)["\']', block)
            if not name_m:
                continue
            clean_name = name_m.group(1).strip()
            if not clean_name or len(clean_name) < 2:
                continue

            career: dict = {}
            career_m = re.search(r'\[(?:"career"|\'career\')\]\s*=\s*\{', block)
            if career_m:
                cb = block.find('{', career_m.end() - 1)
                cd, cj = 1, cb + 1
                while cj < len(block) and cd > 0:
                    if block[cj] == '{': cd += 1
                    elif block[cj] == '}': cd -= 1
                    cj += 1
                career_block = block[cb + 1:cj - 1]
                for cm in re.finditer(r'\[(\d+)\]\s*=\s*([\d.]+)', career_block):
                    career[int(cm.group(1))] = float(cm.group(2))

            players[clean_name] = {
                "credits": float(credit_m.group(1)),
                "ucid":    ucid,
                "career":  career,
            }

    return dict(sorted(players.items(), key=lambda x: x[1]["credits"], reverse=True))



# Tracks which target file paths have already logged a write-failure
# warning, so repeated failures (e.g. every ~5min cycle on a remote-agent
# instance running an older DCSServerBot) log a clear explanation ONCE,
# then drop to debug-level noise instead of spamming ERROR forever.
# Cleared automatically the next time a write to that same path succeeds.
_dedup_write_warned: set[str] = set()

# Logged once, globally, the first time we detect the installed
# DCSServerBot predates the new node.write_file(target, source, overwrite)
# API (3.0.4.28+) — separate from _dedup_write_warned, which tracks
# per-file write FAILURES. This one just informs that an update would
# unlock full remote-node compatibility, even while the local fallback
# below is working fine for this (local) instance.
_old_dcssb_api_warned = False

# Tracks which saves_dir paths have already had their one-time write
# self-test run this bot session, so it only ever runs once per instance
# per process lifetime — not on every update cycle.
_write_self_tested: set[str] = set()


async def write_bytes_to_node(node, target_path: str, data: bytes, log=None) -> bool:
    """Write arbitrary content to `target_path` on `node`, working correctly
    whether that node is the local/master node or a genuinely remote agent
    node, with a safe fallback for older DCSServerBot installs.

    Priority order:
    1. New-style node.write_file(target, source, overwrite) — added in
       DCSServerBot 3.0.4.28 (confirmed with Special K). `source` here is a
       LOCAL file path (not a URL, not raw bytes) — DCSSB itself handles
       copying it to the target node (shutil.copy2 for local, or via its
       internal file-transfer mechanism for remote). We write our content
       to a local temp file first, then hand that path to node.write_file().
       This is the only path that can reach a genuinely remote node.
    2. If that fails — either because the installed DCSSB predates this
       API (old signature is write_file(filename, url, overwrite), so
       calling with target=/source= keyword args raises TypeError), or for
       any other reason — fall back to a plain local open()/os.replace()
       write. This only ever reaches the local/master node's own
       filesystem, but is proven reliable there on any DCSSB version.
    3. If BOTH fail, this is almost certainly a genuinely remote node on a
       DCSServerBot version that doesn't have the new write_file yet —
       nothing we do locally can reach it. Logs a clear one-time hint to
       update DCSServerBot to 3.0.4.28+ (as of writing, on the 'dev'
       branch) rather than a bare, confusing OS error.
    """
    import tempfile

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
            log.debug(f"FH_Report: node.write_file (new API) returned {status!r} for {target_path}")
    except TypeError:
        # Old DCSSB signature (filename, url, overwrite) doesn't accept
        # target=/source= keyword args — this install predates the new API.
        # The local fallback below still works fine for local/master-node
        # instances, but inform once (not a failure — just a heads-up) that
        # updating DCSServerBot would unlock full remote-node compatibility.
        global _old_dcssb_api_warned
        if not _old_dcssb_api_warned:
            _old_dcssb_api_warned = True
            if log:
                log.info(
                    "FH_Report: detected an older DCSServerBot version (older "
                    "than 3.0.4.28) — falling back to local file writes, which "
                    "work fine for local/master-node instances. Update "
                    "DCSServerBot to 3.0.4.28 or later to enable full "
                    "remote-agent-node compatibility for FH_Report's file-write "
                    "features. This message won't repeat."
                )
    except Exception as e:
        if log:
            log.debug(f"FH_Report: node.write_file (new API) failed for {target_path}: {e}")
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
                    f"FH_Report: could not write {target_path} via either the new "
                    f"node.write_file() API or a local write ({e}). If this "
                    f"instance runs on a remote agent node, please update "
                    f"DCSServerBot to 3.0.4.28 or later (currently on the 'dev' "
                    f"branch as of writing) — it adds the remote file-write "
                    f"support FH_Report needs for this. This message won't repeat "
                    f"until the write succeeds, or fails again after that."
                )
        else:
            if log:
                log.debug(f"FH_Report: write to {target_path} failed again: {e}")
        return False


async def run_write_self_test(node, saves_dir: str, log=None) -> None:
    """Proactively verify that write_bytes_to_node actually works for this
    instance, once per bot session — instead of only discovering a write
    problem the next time something genuinely needs correcting (a callsign
    dedup, an inactivity penalty), which could be a long wait and would
    otherwise surface the failure at an inconvenient, hard-to-reproduce
    moment. Writes, reads back, then deletes a tiny throwaway file directly
    under saves_dir — never touches anything Foothold itself owns, and
    matches deduplicate_ranks's own write location (our most frequent real
    write), rather than saves_dir/.fhc/ (which wouldn't exist yet for an
    instance that hasn't triggered daily-tracking's own .fhc creation, and
    would leave a needless empty folder behind for instances that never
    do). Reuses the exact same write_bytes_to_node() path real writes use,
    so this exercises precisely the mechanism we care about, not a
    separate/parallel check.

    Before attempting anything, actively confirms the save folder itself
    exists (a read, via list_directory — never assumed from the write
    failing). If the mission/server has simply never run yet, Foothold
    hasn't created saves_dir at all, and ANY write into it — ours included —
    would fail with a plain "path not found", which is not a real write
    problem at all. That specific, confirmed case is logged at DEBUG (not
    ERROR) and is NOT marked as tested, so it retries on a later cycle once
    the folder actually exists. Any other failure while checking (timeout,
    permission, etc.) is inconclusive — it does NOT get treated as "folder
    missing", since that could silently mask a real write problem; the
    self-test proceeds normally in that case instead."""
    if saves_dir in _write_self_tested:
        return

    try:
        await node.list_directory(saves_dir)
    except FileNotFoundError:
        if log:
            log.debug(
                f"FH_Report: write self-test skipped for {saves_dir} — the save "
                f"folder doesn't exist yet (mission/server probably hasn't run "
                f"yet). Will retry on a later cycle once it exists."
            )
        return
    except Exception:
        # Inconclusive (timeout, permission issue, etc.) — don't assume the
        # folder is missing; fall through and run the self-test as normal.
        pass

    _write_self_tested.add(saves_dir)

    test_path = os.path.join(saves_dir, "fhrep_write_test.tmp")
    test_content = b"FH_Report write self-test - safe to delete"

    if log:
        log.debug(f"FH_Report: running one-time write self-test for {saves_dir}")

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
                    f"FH_Report: write self-test for {saves_dir} wrote successfully "
                    f"but read back different content than expected — file writes "
                    f"may not be fully reliable for this instance."
                )
        elif log:
            log.debug(f"FH_Report: write self-test for {saves_dir} passed (write + read-back verified)")
    except Exception as e:
        if log:
            log.debug(f"FH_Report: write self-test for {saves_dir}: could not read back test file: {e}")

    # Best-effort cleanup — only works for local/master nodes (no confirmed
    # remote-delete API exists on `node`, and we don't invent one just for
    # this). A leftover test file on a genuinely remote node is harmless;
    # not worth a whole new mechanism just to remove it there too.
    try:
        os.remove(test_path)
    except OSError:
        pass


async def deduplicate_ranks(ranks_file: str, persistence_file, node,
                            ranks_source: bytes | None = None) -> bool:
    """Detect and fix duplicate player entries in Foothold_Ranks.lua caused by
    callsign changes. The entry with a UCID in ucidToName is canonical; its
    name is cleaned via strip_callsign(). Credits and lastSeen are merged.
    Returns True if any fix was applied and the file was rewritten."""

    if ranks_source is None:
        ranks_source = await node.read_file(ranks_file)
    ranks_data           = ranks_source.decode("utf-8")
    _original_ranks_data = ranks_data  # snapshot for the pre-write collision check below

    # ── ucidToName: build ucid → raw_name ────────────────────────────────
    ucid_to_raw: dict[str, str] = {}
    for m in re.finditer(r"\[['\"]([a-f0-9]{32})['\"]\]=['\"]([^'\"]+)['\"]", ranks_data):
        ucid_to_raw[m.group(1)] = m.group(2)

    # ── players block: parse name → {credits, lastSeen} via brace counting ─
    players_data: dict[str, dict] = {}
    pos = 0
    while pos < len(ranks_data):
        km = re.search(r"\[['\"]([^'\"]+)['\"]\]=\{", ranks_data[pos:])
        if not km:
            break
        name      = km.group(1)
        brace_pos = pos + km.end() - 1
        depth     = 1
        j         = brace_pos + 1
        while j < len(ranks_data) and depth > 0:
            if ranks_data[j] == "{":   depth += 1
            elif ranks_data[j] == "}": depth -= 1
            j += 1
        block = ranks_data[brace_pos + 1:j - 1]
        cr_m  = re.search(r'[\x27\x22]credits[\x27\x22]\]\s*=\s*([\d.]+)', block)
        ls_m  = re.search(r'[\x27\x22]lastSeen[\x27\x22]\]\s*=\s*([\d.]+)', block)
        if cr_m and len(name) >= 2:
            players_data[name] = {
                "credits":  float(cr_m.group(1)),
                "lastSeen": float(ls_m.group(1)) if ls_m else 0.0,
            }
        pos = pos + km.start() + 1

    # ── Group by strip_callsign base name ─────────────────────────────────
    base_to_raws: dict[str, list] = {}
    for raw in players_data:
        base = strip_callsign(raw)
        base_to_raws.setdefault(base, []).append(raw)

    duplicates = {b: r for b, r in base_to_raws.items() if len(r) > 1}
    if not duplicates:
        return False

    raw_to_ucid = {v: k for k, v in ucid_to_raw.items()}
    modified    = False

    for base_name, raw_names in duplicates.items():
        names_with_ucid = [n for n in raw_names if n in raw_to_ucid]

        # Only treat this as "one real person renamed" if EXACTLY ONE raw
        # name in the group still has a live UCID mapping — the others are
        # then genuinely orphaned leftovers from a past callsign change,
        # safe to fold into the live one. If MORE than one raw name has its
        # own current UCID, these are actually different real players who
        # simply share the same stripped base name (e.g. a squadron tag
        # like "82 TF AA") — merging them would silently combine two
        # distinct players' credits and delete one of their identities.
        # Confirmed with real data: this exact case (two separate live
        # UCIDs both ending in "| 82 TF AA") was actually happening.
        if len(names_with_ucid) != 1:
            if len(names_with_ucid) > 1:
                import logging as _lg3
                _lg3.getLogger(__name__).debug(
                    f"FH_Report: deduplicate_ranks: '{base_name}' has "
                    f"{len(names_with_ucid)} raw names each with their own "
                    f"live UCID ({names_with_ucid}) — treating as distinct "
                    f"players who share a stripped base name, not a rename. "
                    f"Skipping merge."
                )
            continue
        name_with_ucid = names_with_ucid[0]

        canonical     = strip_callsign(name_with_ucid)
        ucid          = raw_to_ucid[name_with_ucid]
        total_credits = sum(players_data[n]["credits"]  for n in raw_names)
        max_last_seen = max(players_data[n]["lastSeen"] for n in raw_names)
        lua_cr        = str(int(total_credits)) if total_credits == int(total_credits) else str(total_credits)

        # ── Remove each raw entry using brace counting ────────────────────
        for raw in raw_names:
            found = False
            for q in ('"', "'"):
                key = f"[{q}{raw}{q}]="
                idx = ranks_data.find(key)
                if idx == -1:
                    continue
                bs = ranks_data.find("{", idx)
                if bs == -1:
                    continue
                depth = 1
                k     = bs + 1
                while k < len(ranks_data) and depth > 0:
                    if ranks_data[k] == "{":   depth += 1
                    elif ranks_data[k] == "}": depth -= 1
                    k += 1
                # Include leading whitespace on the line
                line_start = ranks_data.rfind("\n", 0, idx)
                start_pos  = line_start + 1 if line_start >= 0 else idx
                # Include trailing comma and newline
                end_pos = k
                while end_pos < len(ranks_data) and ranks_data[end_pos] in (",", "\r", "\n", " "):
                    end_pos += 1
                ranks_data = ranks_data[:start_pos] + ranks_data[end_pos:]
                found = True
                break
            if not found:
                import logging as _lg2
                _lg2.getLogger(__name__).warning(
                    f"FH_Report: deduplicate_ranks: could not find entry for '{raw}' to remove"
                )

        # ── Insert canonical entry ────────────────────────────────────────
        new_entry  = f'  ["{canonical}"]=\n    ["credits"]={lua_cr},\n    ["lastSeen"]={max_last_seen},\n  ,\n'
        new_entry  = '  ["' + canonical + '"]={\n    ["credits"]=' + lua_cr + ',\n    ["lastSeen"]=' + str(max_last_seen) + ',\n  },\n'
        insert_pat = r'(RankSave\[[\'\"]players[\'\"]\]\s*=\s*\{)'
        ranks_data = re.sub(insert_pat, r'\1\n' + new_entry, ranks_data, count=1)

        # ── Update ucidToName ─────────────────────────────────────────────
        for q in ('"', "'"):
            old_e = f'[{q}{ucid}{q}]={q}{name_with_ucid}{q}'
            if old_e in ranks_data:
                ranks_data = ranks_data.replace(old_e, f'["{ucid}"]="{canonical}"', 1)
                break

        modified = True
        import logging as _lg
        _lg.getLogger(__name__).info(
            f"FH_Report: merged duplicate entries {raw_names} -> '{canonical}' "
            f"(credits: {total_credits}, lastSeen: {max_last_seen})"
        )

    if not modified:
        return False

    # Re-read right before writing to detect if Foothold itself wrote to
    # this file in the meantime, and skip this cycle's write rather than
    # risk clobbering a newer version — the next cycle will simply retry.
    recheck = (await node.read_file(ranks_file)).decode("utf-8")
    if recheck != _original_ranks_data:
        import logging as _lg2
        _lg2.getLogger(__name__).warning(
            f"FH_Report: {ranks_file} changed since read (likely written by "
            f"Foothold) — skipping deduplication this cycle, will retry next."
        )
        return False

    import logging as _lg2
    return await write_bytes_to_node(node, ranks_file, ranks_data.encode("utf-8"), log=_lg2.getLogger(__name__))


def _is_numeric_segment(s: str) -> bool:
    """Return True if a name segment is mainly numeric (>49% digits/separators).
    Counts digits, hyphens and underscores as numeric characters.
    Catches slot/squadron identifiers like 307, 305A, A305, 3-7, VFA-75, F_16."""
    s = s.strip()
    if not s:
        return False
    numeric = sum(1 for c in s if c.isdigit() or c in '-_')
    return numeric / len(s) > 0.49


def strip_callsign(name: str) -> str:
    """Remove flight callsign prefix from pilot name.
    Handles separators (|, /, backslash, ,, ' - ') and callsign patterns (WORD N-N).
    Preserves squadron tags like [MA] at the start.
    When two or more | separators are present and the last segment is mainly
    numeric (slot number like 307, 305A), the second-to-last segment is used
    instead — e.g. 'GUNSTAR 11 | DRCHOW | 307' → 'DRCHOW'."""
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
    import re as _re
    callsign_pattern = _re.compile(r'^[A-Z][A-Z0-9]* \d+-\d+\s*', _re.IGNORECASE)
    stripped = callsign_pattern.sub('', name).strip()
    # Only apply if result is not empty
    if stripped:
        name = stripped

    return name.strip()


def _safe_code_span(text: str) -> str:
    """Wrap arbitrary text in a Markdown code span that is guaranteed to
    render correctly no matter what characters it contains — backticks,
    backslashes, quotes, asterisks, underscores, tildes, pipes, etc.

    A Markdown/Discord code span does not interpret ANY formatting or
    backslash-escapes inside it — the only character that matters is the
    backtick itself, because a run of N backticks closes a fence opened by
    a run of N (or fewer) backticks. Per the CommonMark rule, using a fence
    one backtick longer than the longest run of consecutive backticks found
    inside the content makes it impossible for the content to accidentally
    close the span early — regardless of anything else it contains.

    A single padding space is added on each side when the content starts or
    ends with a backtick (or is empty/whitespace-only), matching the
    CommonMark convention, so the fence never visually merges with it.
    Ordinary names (the overwhelming majority) are unaffected: they get the
    same single-backtick wrap as before.
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
    """Dynamically shorten `rank` (from the tail, adding '..') just enough
    to keep the full rendered line under `threshold` visible characters, as
    the reader actually sees it — i.e. ignoring Markdown syntax like ** and
    backticks, which take no visual width. `prefix` is everything visible
    before the rank (name plus its separator); `suffix` is everything
    visible after it (its own separator plus the points text). Measured
    against Discord's DESKTOP client specifically (mobile line-wrap width
    is unpredictable and out of scope here).

    Chosen over a fixed rank-abbreviation table so it also covers
    hook-supplied custom_rank text, which a static table could never
    anticipate. Below the threshold, or for short ranks that don't need
    it, `rank` is returned unchanged.
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
                        min3_latest_day: bool = False) -> str | None:
    """Build the Podium table, grouped by closing event (date + optional
    Session End marker), each showing the top `top` positions (1-50) that
    day — NOT a single position, the top N positions.

    history:   the full daily_history.json dict {date_str: [event, ...]}
    players:   current parsed roster {name: {credits, custom_rank, ...}} —
               used to look up each entry's CURRENT rank (via custom_rank
               if the fh_hook.yaml override is set, else get_rank() from
               current credits), matching how every other table resolves
               rank — not a frozen rank from the day it happened, since a
               player's rank keeps climbing and freezing it would show
               stale titles for old entries.
    days:      0 = all available history; otherwise only the most recent
               N calendar dates that have at least one event. This is the
               only size control here — there's no separate line cap.
               Real Discord limits (1024 chars/field, 25 fields/embed) are
               handled downstream by _add_podium_field's chunking, which
               truncates whole blocks with a "+ N more" note if needed
               rather than cutting a block awkwardly mid-way. A dedicated
               max_lines option was tried and removed: with `top` able to
               go up to 50, a single event could need 51 lines on its own,
               making any modest line cap truncate mid-block on essentially
               every render — the opposite of what it was meant to prevent.
    top:       show the top N positions (1-50) for each closing event —
               e.g. top=3 shows 1st, 2nd AND 3rd place, not just 3rd.
    strip_callsign_flag: mirrors the same option used by every other table,
               for visual consistency.
    min3_latest_day: if True, every event under the single most recent date
               (dates_desc[0] — both closures if that day had two) shows at
               least the top 3 positions, even if `top` is set lower (1 or
               2). `top` itself is never reduced by this — if top is already
               >= 3, this has no effect. Only ever passed True when "P" is
               combined with other letters (podium_combined_min3_latest_day);
               the standalone "P" mode never uses this.

    Each event renders as:
        __**DD/MM/YYYY**__ (Session End)      <- suffix only on campaign-end closures
        🥇 `Name` — **Rank** — N,NNN pts
        🥈 `Name` — **Rank** — N,NNN pts
        🎖️ `Name` — **Rank** — N,NNN pts      <- 4th place onward
    Blocks are separated by a blank line. A player no longer in the current
    roster is shown without a rank part.

    Returns None if there's no history at all, or nothing to show at any
    requested position — the section is then skipped entirely, same
    cycle-skip convention as every other table."""
    if not history:
        return None

    dates_desc = sorted(history.keys(), reverse=True)
    if days and days > 0:
        dates_desc = dates_desc[:days]

    medals = ["🥇", "🥈", "🥉"]
    blocks: list[list[str]] = []
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
                display = strip_callsign(name) if strip_callsign_flag else name
                short   = _safe_code_span(display)
                player_data = players.get(name)
                if not player_data:
                    # No exact-name match — playerStats/history may record
                    # the name without the flight-callsign prefix that
                    # Foothold_Ranks.lua (and therefore `players`) carries,
                    # or vice versa. Fall back to a callsign-stripped
                    # comparison, same as the Session/Daily leaderboard
                    # lookups above, before giving up on a rank entirely.
                    for p_name, p_data in players.items():
                        if strip_callsign(p_name) == strip_callsign(name):
                            player_data = p_data
                            break
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


def _add_podium_field(embed: discord.Embed, icon: str, podium_text: str) -> None:
    """Add the Podium table to the embed, chunked across multiple fields if
    needed — same FIELD_LIMIT-based chunking pattern already used for the
    pilot leaderboard tables, since a single Discord embed field has a hard
    1024-character limit that a long Podium listing (many days, and/or long
    player names/rank titles) could otherwise exceed and get the whole
    embed rejected by Discord instead of silently trimmed.

    Also guards Discord's SEPARATE hard limit of 25 fields per embed —
    unrelated to the 6000-character total handled by _trim_embed, and not
    covered by chunking alone. If adding all Podium chunks would exceed
    that cap given how many fields the embed already has (zones, leaderboard
    tables, etc.), the listing is truncated with a "+ N more" note instead
    of letting Discord reject the whole embed. A couple of field slots are
    reserved for whatever gets added after Podium (the closing ruler, at
    minimum) so this doesn't just shift the overflow one step later."""
    MAX_EMBED_FIELDS    = 25
    RESERVED_FOR_TRAILER = 2

    title = f"{icon} __Daily Podium__"
    cont_title = f"{icon} __Daily Podium (cont.)__"
    lines = podium_text.split("\n")
    FIELD_LIMIT = 1020
    chunks, cur, cur_len = [], [], 0
    for line in lines:
        ll = len(line) + 1
        if cur_len + ll > FIELD_LIMIT and cur:
            chunks.append("\n".join(cur))
            cur, cur_len = [line], ll
        else:
            cur.append(line)
            cur_len += ll
    if cur:
        chunks.append("\n".join(cur))

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


def _build_pilot_card(career: dict, icon: str = "🔸") -> str | None:
    """Build a one-line pilot career card from career stats dict.
    CAREER_STAT IDs (Foothold v4.5):
      1=FlightSeconds  3=HelicopterSeconds  8=ConventionalCarrierTraps
      10=TotalKills    21=PilotDeaths       30=FuelReceivedLbs
    Data sourced from Foothold_Ranks.lua — historical career totals only.
    Returns None if all values are zero."""
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

    def _fmt_compact(n: int) -> str:
        """Format a number compactly: <1000 exact, 1k-999k with 1 decimal
        (stripped if .0), >=1M in millions likewise. Used to keep long values
        (e.g. fuel in lbs) from making the line too wide."""
        if n < 1000:
            return str(n)
        if n < 1_000_000:
            s = f"{n / 1000:.1f}".rstrip("0").rstrip(".")
            return f"{s}k"
        s = f"{n / 1_000_000:.1f}".rstrip("0").rstrip(".")
        return f"{s}M"

    parts = []
    fixed_str = _fmt_time(fixed_s)
    if fixed_str: parts.append(f"{fixed_str} Fixed")
    helo_str  = _fmt_time(helo_s)
    if helo_str:  parts.append(f"{helo_str} Helo")
    if kills > 0:      parts.append(f"{kills} Kills")
    if traps > 0:      parts.append(f"{traps} Traps")
    if refuel_lbs > 0: parts.append(f"{_fmt_compact(refuel_lbs)} lbs")
    if deaths > 0:     parts.append(f"{deaths} Deaths")

    if not parts:
        return None
    return f"·　{icon} " + " · ".join(parts)


def _build_session_card(raw_stats: dict, icon: str = "🔸") -> str | None:
    """Build a one-line session/daily stats card from raw playerStats keys,
    using the confirmed correlation between playerStats (session) and
    CAREER_STAT (career) fields in Foothold's source. Kills are grouped into
    four categories:
      Air     = Air + Helo (aircraft kills)
      SAM     = SAM (air defense kills)
      Ground  = Ground Units + Structure + Infantry
      Ship    = Ship (naval kills)
    'Missions' sums only keys whose name contains the word "mission"
    (case-insensitive) — e.g. CAP mission, SEAD mission, CAS mission.
    'Achievement' is Foothold's own milestone-unlock counter (playerStats key
    'Achievement', confirmed via zoneCommander.lua's STATS_LABEL_ACHIEVEMENT) —
    a progression/summary stat rather than raw combat action, so it's ranked
    right after Missions and ahead of the combat kill categories.
    Plus Rescues (Pilot Rescue), Refuels (Refueling event count), and Deaths.
    Displayed as 'Msn', 'Ach' and 'Resc' respectively (abbreviated to keep the
    line short enough to avoid Discord's mobile-width wraparound).
    Priority order (highest to lowest): Missions, Achievement, Air, SAM,
    Ground, Ship, Rescues, Refuels, Deaths. Capped at 7 fields — lowest-
    priority fields are dropped first if there are more than 7 with a
    non-zero value. Deaths is always shown if > 0.
    Note: 'Flight time' is intentionally excluded — Foothold only records it
    for a specific aircraft whitelist (mostly helicopters/transports, see
    LogisticCommander.AllowedFlightTimeReward), so it reads 0/absent for
    conventional fixed-wing combat aircraft even after long flights. Showing
    it would be misleading for the majority of players. Career's FlightSeconds/
    HelicopterSeconds (used in the pilot card) does not have this limitation.
    Values of zero are omitted. Returns None if all values are zero."""
    if not raw_stats:
        return None

    missions = sum(
        int(v) for k, v in raw_stats.items()
        if "mission" in k.lower() and isinstance(v, (int, float)) and v > 0
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
        (8, f"{deaths} Death" + ("s" if deaths != 1 else "")) if deaths > 0 else None,
    ]
    candidates = [c for c in candidates if c is not None]

    # Cap at 7 fields — drop lowest-priority fields first, but always keep deaths
    if len(candidates) > 7:
        deaths_entry = next((c for c in candidates if c[0] == 8), None)
        others       = [c for c in candidates if c[0] != 8]
        keep_count   = 6 if deaths_entry else 7
        others       = sorted(others, key=lambda c: c[0])[:keep_count]
        candidates   = sorted(others + ([deaths_entry] if deaths_entry else []), key=lambda c: c[0])

    parts = [label for _, label in candidates]

    if not parts:
        return None
    return f"·　{icon} " + " · ".join(parts)


# Fixed priority tiers for individual playerStats keys in the full-detail
# Session/Daily Stats sections of /fh_report player, mirroring the same
# conceptual grouping order as the main embed's compact card (_build_session_card)
# — but keeping every individual key visible rather than collapsing them into
# summed categories. Keys containing "mission" (any case) always sort into
# tier 0 regardless of their exact name. Deaths is always forced to the end.
_STAT_KEY_ORDER = [
    "Achievement", "Air", "Helo", "SAM",
    "Infantry", "Ground Units", "Structure",
    "Ship", "Pilot Rescue", "Refueling",
]


def _display_stat_label(key: str) -> str:
    """Friendlier display label for specific raw playerStats keys shown in
    /fh_report player's full-detail Session/Daily Stats. 'Flight time' is
    Foothold's own landing-triggered counter, limited to a whitelist of
    helicopters and a few transport aircraft (see AllowedFlightTimeReward
    in zoneCommander.lua) — nothing to do with total flight hours (that's
    Career Stats' Flight Hours fixed/helo, which has no such limitation).
    Relabeling avoids the key being misread as total time flown this session."""
    if key == "Flight time":
        return "Transport Flight Time"
    return key


def _display_stat_value(key: str, value: float) -> str:
    """Unit-aware formatting for specific raw playerStats keys, to avoid
    ambiguity about what the raw number represents:
    - 'Flight time' is recorded in minutes (see zoneCommander.lua's
      addTempStat(player,'Flight time',minutes,crew)) — shown as 'Xh Ym'
      instead of a bare number that could be misread as hours.
    - 'Refueling' is a count of in-flight refueling events, not fuel
      quantity (career's Fuel Received, shown in lbs, is the separate
      quantity figure) — shown as 'N event(s)' to avoid that confusion.
    Everything else uses the normal numeric formatting."""
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
    full-detail stats sections: Missions, Achievement, Air, Helo, SAM,
    Infantry, Ground Units, Structure, Ship, Pilot Rescue, Refueling, any
    unrecognized keys (alphabetical), then Deaths always last."""
    def rank(key: str) -> tuple[int, str]:
        if key == "Deaths":
            return (99, key)
        if "mission" in key.lower():
            return (0, key)
        if key in _STAT_KEY_ORDER:
            return (_STAT_KEY_ORDER.index(key) + 1, key)
        return (98, key)  # unrecognized — after known categories, before Deaths

    return sorted(stats.items(), key=lambda kv: rank(kv[0]))


def _build_player_report_embed(player_name: str, data: dict, ucid: str | None,
                               last_seen, session_points: float,
                               daily_points: float, session_stats: dict,
                               mission_status: str,
                               daily_stats: dict | None = None) -> discord.Embed:
    """Build a read-only, info-only player embed for /fh_report player.
    No buttons, no editing — mirrors FH_Control's player embed sections
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
    embed.add_field(name="\u200b", value="─" * 32, inline=False)
    if last_seen is not None:
        import calendar as _cal
        ts = int(_cal.timegm(last_seen.timetuple()))
        activity_line = f"- **Last seen:** <t:{ts}:F> (<t:{ts}:R>)"
    else:
        activity_line = "- **Last seen:** —"
    embed.add_field(name="🕒 __Activity__", value=activity_line, inline=False)

    # ── Daily Stats (full, unfiltered — same level of detail as Session Stats) ─
    # Only non-zero fields are shown; if everything is zero the section is
    # omitted entirely (not even a placeholder), per the original spec.
    # Daily Points shown in the section title itself, not as a separate block.
    if daily_stats:
        daily_filtered = {k: v for k, v in daily_stats.items() if k != "Points" and v}
        if daily_filtered:
            daily_ordered = _order_stat_items(daily_filtered)
            daily_lines = "\n".join(f"- **{_display_stat_label(k)}:** {_display_stat_value(k, v)}" for k, v in daily_ordered)
            embed.add_field(name="\u200b", value="─" * 32, inline=False)
            embed.add_field(name=f"📅 __Daily Stats__ (D: {_fmt_num(daily_points)})",
                            value=daily_lines, inline=False)

    # ── Session Stats (full, unfiltered) ───────────────────────────────
    # Session Points shown in the section title itself.
    embed.add_field(name="\u200b", value="─" * 32, inline=False)
    session_title = f"📊 __Session Stats__ (S: {_fmt_num(session_points)})"
    if session_stats:
        other_stats = {k: v for k, v in session_stats.items() if k != "Points"}
        if other_stats:
            other_ordered = _order_stat_items(other_stats)
            stat_lines = "\n".join(f"- **{_display_stat_label(k)}:** {_display_stat_value(k, v)}" for k, v in other_ordered)
            embed.add_field(name=session_title, value=stat_lines, inline=False)
        else:
            embed.add_field(name=session_title,
                            value="_No stats yet — will appear after first flight._", inline=False)
    else:
        embed.add_field(name=session_title,
                        value="_No stats yet — will appear after first flight._", inline=False)

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
    if career.get(CAREER_TRAPS, 0) > 0:
        career_lines.append(f"- **Carrier Traps:** {int(career[CAREER_TRAPS])}")
    if career.get(CAREER_FUEL_LBS, 0) > 0:
        fuel_lbs = int(career[CAREER_FUEL_LBS])
        from math import trunc as _trunc
        _fuel_str = str(fuel_lbs) if fuel_lbs < 1000 else (
            f"{fuel_lbs/1000:.1f}".rstrip("0").rstrip(".") + "k" if fuel_lbs < 1_000_000 else
            f"{fuel_lbs/1_000_000:.1f}".rstrip("0").rstrip(".") + "M"
        )
        career_lines.append(f"- **Fuel Received:** {_fuel_str} lbs")
    if career.get(CAREER_DEATHS, 0) > 0:
        career_lines.append(f"- **Pilot Deaths:** {int(career[CAREER_DEATHS])}")
    if career_lines:
        embed.add_field(name="\u200b", value="─" * 32, inline=False)
        embed.add_field(
            name=f"🏆 __Career Stats__ (R: {_fmt_num(credits)} — {get_rank(credits)})",
            value="\n".join(career_lines), inline=False)

    # ── Mission ─────────────────────────────────────────────────────────
    embed.add_field(name="\u200b", value="─" * 32, inline=False)
    embed.add_field(name="🖥️ __Mission__", value=mission_status, inline=False)

    embed.set_footer(text=f"FH_Report {FH_REPORT_RELEASE} · Read-only player report")
    return embed


# ── report_layout engine ──────────────────────────────────────────────────────
# Generalizes the old fixed set of points_order codes (R, S, D, BR, BS, BD,
# BDS, 2R, 2S, 2D, 2DS, 3R, 3S, 3D, 3DS, 4R, 4DS, P, T) into a small
# composable grammar: report_layout is a string of D/P/S/R letters, in any
# order and any subset, naming which tables to render and in what sequence.
# points_detail_D/points_detail_S/points_detail_R independently control
# EXACTLY what each table shows, in the order given — nothing is implied:
# if you want a table's own value shown, its own letter must be in its
# own points_detail_<role> string. A role with no points_detail_<role> key
# at all shows only its own value (e.g. no points_detail_S = "(S: nnn)"),
# same as every other show_*-style feature in this plugin defaulting off.
# Legacy points_order/compact_points values (and the older shared
# points_detail single-string format from before this per-table split)
# are translated into this grammar once, permanently, by
# migrate_config.py when the config is migrated — this file only ever
# reads report_layout/points_detail_D/points_detail_S/points_detail_R
# directly (see FHReport._resolve_report_layout() in the cog below).

_ROLE_TITLES = {
    "D": "📅 __Daily Leaderboard · by Today's Points__",
    "S": "📊 __Session Leaderboard · by Current Session__",
    "R": "🏆 __Pilot Leaderboard · by Rank__",
}
_ROLE_ICONS = {"D": "📅", "S": "📊", "R": "🏆"}


def _emit_table_field(embed: discord.Embed, title: str, icon: str,
                      lines: list[str], hidden: int, show_all_pilots: bool) -> None:
    """Add one leaderboard table's lines to the embed. Mirrors the two
    field-limit strategies used throughout this file's legacy table code:
    show_all_pilots=True chunks into multiple fields with '__Pilots #a-b__'
    continuation headers so nobody is ever silently dropped; False cuts at
    Discord's ~1020-char field limit and appends a single '+ N more pilots'
    note instead."""
    if not lines:
        return
    FIELD_LIMIT = 1020
    if show_all_pilots:
        all_lines = list(lines)
        if hidden > 0:
            all_lines.append(f"*+ {hidden} more pilots*")
        is_more_note = lambda line: line.startswith("*+ ") and line.endswith(" more pilots*")
        chunks, cur, cur_len, cur_count = [], [], 0, 0
        for line in all_lines:
            ll = len(line) + 1
            if cur_len + ll > FIELD_LIMIT and cur:
                chunks.append((cur, cur_count))
                cur, cur_len = [line], ll
                cur_count = 0 if is_more_note(line) else 1
            else:
                cur.append(line)
                cur_len += ll
                if not is_more_note(line):
                    cur_count += 1
        if cur:
            chunks.append((cur, cur_count))
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
    embed: discord.Embed, report_layout: str, points_detail: dict,
    players: dict, dp: dict, has_daily: bool, strip_callsign_flag: bool,
    max_pilots: int | None, max_pilots_2t: int | None, max_pilots_3t: int | None,
    show_all_pilots: bool,
    show_pilot_card: bool, pilot_card_icon: str,
    show_session_card: bool, session_card_icon: str,
    show_daily_card: bool, daily_card_icon: str,
    show_punishment: bool, punishment_points: dict | None,
    daily_history: dict | None,
    podium_days: int, podium_top: int,
    podium_combined_days: int, podium_combined_top: int, podium_combined_min3_latest_day: bool,
) -> None:
    """Render every table/Podium named in report_layout, in order, onto
    embed. `points_detail` is a dict {"D": "...", "S": "...", "R": "..."} —
    a role missing from it shows only its own value. See the module-level
    comment above for the grammar. Mutates embed in place — mirrors
    _add_podium_field's own convention."""
    layout = (report_layout or "R").strip().upper()
    points_detail = points_detail or {}
    roles_in_layout = [c for c in layout if c in ("R", "S", "D")]

    # Punishment badge goes on the first table whose role has the highest
    # priority (R > S > D) among roles actually present — same R>S>D
    # priority the legacy multi-table modes always used.
    _priority = {"R": 0, "S": 1, "D": 2}
    badge_role = min(roles_in_layout, key=lambda r: _priority[r]) if roles_in_layout else None
    badge_role_done = False

    pp = punishment_points or {}
    surplus = 0

    # Every table in the composition shares the SAME cap — determined by
    # the total number of pilot tables (R/S/D letters, Podium doesn't
    # count) in the layout — falling back down the chain
    # max_pilots_3t -> max_pilots_2t -> max_pilots, exactly as the legacy
    # 2x/3x/4x modes did (a single-table layout just uses max_pilots
    # alone). Surplus still cascades between tables when one has fewer
    # players than the cap allows.
    _table_count = len(roles_in_layout)
    if _table_count <= 1:
        table_limit = max_pilots
    elif _table_count == 2:
        table_limit = max_pilots_2t or max_pilots
    else:
        table_limit = max_pilots_3t or max_pilots_2t or max_pilots

    for letter in layout:
        if letter == "P":
            if layout == "P":
                p_lines = _build_podium_table(
                    daily_history or {}, players, days=podium_days, top=podium_top,
                    strip_callsign_flag=strip_callsign_flag
                )
            else:
                p_lines = _build_podium_table(
                    daily_history or {}, players, days=podium_combined_days, top=podium_combined_top,
                    strip_callsign_flag=strip_callsign_flag, min3_latest_day=podium_combined_min3_latest_day
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

        limit = table_limit
        total_items = len(items)
        limit_eff = (limit + surplus) if limit else None
        if limit_eff:
            items = items[:limit_eff]
        surplus = max(0, limit_eff - len(items)) if limit_eff else 0
        hidden = total_items - len(items)

        medals = ["🥇", "🥈", "🥉"] + ["🎖️"] * 50
        lines = []
        for i, (name, data) in enumerate(items):
            credits = int(data["credits"])
            rank    = data.get("custom_rank") or get_rank(credits)
            display = strip_callsign(name) if strip_callsign_flag else name
            short_src = display if len(display) <= 22 else display[:20] + '..'
            short   = _safe_code_span(short_src)
            medal   = data.get("custom_medal") or (medals[i] if i < len(medals) else "•")

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
                card = _build_pilot_card(data.get("career") or {}, icon=pilot_card_icon)
                if card:
                    block.append(card)
            if show_session_card and role == "S":
                s_card = _build_session_card(data.get("session_stats") or {}, icon=session_card_icon)
                if s_card:
                    block.append(s_card)
            if show_daily_card and role == "D":
                d_card = _build_session_card(data.get("daily_stats") or {}, icon=daily_card_icon)
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


def build_embed(zones: dict, players: dict, campaign_name: str,
                max_zones: int | None, max_pilots: int | None,
                bar_length: int, slot_status: bool = False,
                zone_name_length: int = 16,
                max_pilots_2t: int | None = None,
                max_pilots_3t: int | None = None,
                punishment_points: dict | None = None,
                show_punishment: bool = False,
                show_all_pilots: bool = False,
                strip_callsign_flag: bool = False,
                campaign_stats: dict | None = None,
                bar_style_emoji: bool = False,
                daily_points: dict | None = None,
                show_pilot_card: bool = False,
                pilot_card_icon: str = "🔸",
                show_session_card: bool = False,
                session_card_icon: str = "🔸",
                session_stats_raw: dict | None = None,
                show_daily_card: bool = False,
                daily_card_icon: str = "🔸",
                daily_stats_raw: dict | None = None,
                player_cmd_hint: str | None = None,
                daily_history: dict | None = None,
                podium_days: int = 7,
                podium_top: int = 1,
                podium_combined_days: int = 7,
                podium_combined_top: int = 1,
                podium_combined_min3_latest_day: bool = False,
                sort_zones_by_waypoint: bool = False,
                waypoint_map: dict | None = None,
                report_layout: str = "R",
                points_detail: dict | None = None) -> discord.Embed:
    """Build the Discord embed from parsed Foothold data."""
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

    # BLUE zones — actives first sorted by level+slots (or by waypoint number
    # if sort_zones_by_waypoint is enabled), suspended last
    blue_active    = [z for z in zones["blue"] if not z.get("suspended")]
    blue_suspended = [z for z in zones["blue"] if z.get("suspended")]
    if sort_zones_by_waypoint and waypoint_map:
        _blue_with_wp    = [z for z in blue_active if z["name"] in waypoint_map]
        _blue_without_wp = [z for z in blue_active if z["name"] not in waypoint_map]
        _blue_with_wp    = sorted(_blue_with_wp, key=lambda z: waypoint_map[z["name"]], reverse=True)
        _blue_without_wp = sorted(_blue_without_wp, key=lambda z: (z["level"], z.get("active_slots", 0)), reverse=True)
        blue_active      = _blue_with_wp + _blue_without_wp
    else:
        blue_active    = sorted(blue_active, key=lambda z: (z["level"], z.get("active_slots", 0)), reverse=True)
    blue_suspended = sorted(blue_suspended, key=lambda z: z["level"], reverse=True)
    blue_sorted    = blue_active + blue_suspended
    limit          = max_zones if max_zones else len(blue_sorted)
    blue_lines     = []
    for z in blue_sorted[:limit]:
        lvl = min(z["level"], 5)
        if slot_status and not z.get("suspended"):
            _blue_total = z.get("true_max") or z["level"]
            display_lvl, display_active = _slot_display_counts(_blue_total, z.get("active_slots", z["level"]))
            stars = "🔹" * display_active + "◇" * (display_lvl - display_active)
        else:
            stars  = "🔹" * lvl
        blue_lines.append(f"`{z['name'][:zone_name_length]}` {stars}")
    if max_zones and len(blue_sorted) > max_zones:
        blue_lines.append(f"*+ {len(blue_sorted) - max_zones} more bases*")
    blue_lines.append(".")
    blue_text = "\n".join(blue_lines) if blue_lines else "—"

    # RED zones — actives first sorted by level+slots (or by waypoint number
    # if sort_zones_by_waypoint is enabled), suspended last
    red_active    = [z for z in zones["red"] if not z.get("suspended")]
    red_suspended = [z for z in zones["red"] if z.get("suspended")]
    if sort_zones_by_waypoint and waypoint_map:
        _red_with_wp    = [z for z in red_active if z["name"] in waypoint_map]
        _red_without_wp = [z for z in red_active if z["name"] not in waypoint_map]
        _red_with_wp    = sorted(_red_with_wp, key=lambda z: waypoint_map[z["name"]])
        _red_without_wp = sorted(_red_without_wp, key=lambda z: (z["level"], z.get("active_slots", 0)), reverse=True)
        red_active      = _red_with_wp + _red_without_wp
    else:
        red_active    = sorted(red_active, key=lambda z: (z["level"], z.get("active_slots", 0)), reverse=True)
    red_suspended = sorted(red_suspended, key=lambda z: z["level"], reverse=True)
    red_sorted    = red_active + red_suspended
    limit         = max_zones if max_zones else len(red_sorted)
    red_lines     = []
    for z in red_sorted[:limit]:
        lvl = min(z["level"], 5)
        if slot_status and not z.get("suspended"):
            display_lvl, display_active = _slot_display_counts(z["level"], z.get("active_slots", z["level"]))
            stars = "🔺" * display_active + "△" * (display_lvl - display_active)
        else:
            stars  = "🔺" * lvl
        red_lines.append(f"`{z['name'][:zone_name_length]}` {stars}")
    if max_zones and len(red_sorted) > max_zones:
        red_lines.append(f"*+ {len(red_sorted) - max_zones} more bases*")
    red_text = "\n".join(red_lines) if red_lines else "—"

    # Embed + zone fields are created here (moved up from just before the
    # tables section) so the report_layout engine below can add its own
    # fields onto the same embed object.
    embed = discord.Embed(
        title=f"📡  {campaign_name}",
        description=(
            f"**Front Status — {timestamp}**\n\n"
            f"{progress}"
        ),
        color=0x3498DB
    )
    # Force both column headers to the same fixed width so the embed always
    # reaches maximum width regardless of zone count digits or content length.
    # The target is the longer of the two headers + 44 spaces + dot (same as
    # the manually tuned RED value). Both headers are padded to that target.
    _blue_hdr  = f"🔵 BLUE Zones ({blue_count})"
    _red_hdr   = f"🔴 RED Zones ({red_count})"
    embed.add_field(
        name=_blue_hdr,
        value=blue_text[:1024],
        inline=True
    )
    embed.add_field(
        name=_red_hdr,
        value=red_text[:1024],
        inline=True
    )

    # Pilot leaderboard — apply session stats and ordering
    cs   = campaign_stats or {}
    srs  = session_stats_raw or {}
    drs  = daily_stats_raw or {}

    # Add session_points to each player
    # Skip if hook already set session_points (hook value takes priority)
    for name, data in players.items():
        if "session_points" not in data:
            s_pts = cs.get(name, 0)
            if s_pts == 0:
                for cs_name, cs_pts in cs.items():
                    if strip_callsign(cs_name) == strip_callsign(name):
                        s_pts = cs_pts
                        break
            data["session_points"] = s_pts
        # Attach raw session stats (kills/missions) for the session card
        if "session_stats" not in data:
            raw = srs.get(name)
            if raw is None:
                for srs_name, srs_val in srs.items():
                    if strip_callsign(srs_name) == strip_callsign(name):
                        raw = srs_val
                        break
            data["session_stats"] = raw or {}
        # Attach raw daily stats (kills/missions delta) for the daily card
        if "daily_stats" not in data:
            draw = drs.get(name)
            if draw is None:
                for drs_name, drs_val in drs.items():
                    if strip_callsign(drs_name) == strip_callsign(name):
                        draw = drs_val
                        break
            data["daily_stats"] = draw or {}

    dp          = daily_points or {}  # {name: daily_pts}
    drs_check   = daily_stats_raw or {}
    has_daily   = bool(dp) or any(drs_check.values())

    _render_layout_tables(
        embed=embed, report_layout=report_layout, points_detail=points_detail,
        players=players, dp=dp, has_daily=has_daily,
        strip_callsign_flag=strip_callsign_flag,
        max_pilots=max_pilots, max_pilots_2t=max_pilots_2t, max_pilots_3t=max_pilots_3t,
        show_all_pilots=show_all_pilots,
        show_pilot_card=show_pilot_card, pilot_card_icon=pilot_card_icon,
        show_session_card=show_session_card, session_card_icon=session_card_icon,
        show_daily_card=show_daily_card, daily_card_icon=daily_card_icon,
        show_punishment=show_punishment, punishment_points=punishment_points,
        daily_history=daily_history,
        podium_days=podium_days, podium_top=podium_top,
        podium_combined_days=podium_combined_days, podium_combined_top=podium_combined_top,
        podium_combined_min3_latest_day=podium_combined_min3_latest_day,
    )

    # Full-width separator — placed at the bottom to fix embed width
    # without interrupting the visual flow of the content.
    try:
        from core import utils as _dcssb_utils
        _ruler_name = _dcssb_utils.print_ruler(ruler_length=34)
    except Exception:
        _ruler_name = "─" * 34
    embed.add_field(name="\u200b", value=_ruler_name, inline=False)
    footer_lines = [f"FH_Report {FH_REPORT_RELEASE}"]
    if player_cmd_hint:
        footer_lines.append(player_cmd_hint)
    footer_lines.append(f"{campaign_name} • Updated automatically")
    embed.set_footer(text="\n".join(footer_lines))
    embed.timestamp = datetime.now(timezone.utc)

    # Trim if embed exceeds Discord 6000 char limit
    embed = _trim_embed(embed)

    return embed


# ── Optional private hook ─────────────────────────────────────────────────────
import importlib.util as _iutil
import os as _os

def _load_hook():
    _hook_path = _os.path.join(_os.path.dirname(__file__), "fh_hook.py")
    if not _os.path.exists(_hook_path):
        return None, False
    try:
        _spec = _iutil.spec_from_file_location("fh_hook", _hook_path)
        _mod  = _iutil.module_from_spec(_spec)
        _spec.loader.exec_module(_mod)
        return _mod, True
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"FH_Report: fh_hook load error: {e}")
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


# ── Rank thresholds (for penalty step calculation) ────────────────────────────
# Must match RANK_THRESHOLDS defined earlier.
_PENALTY_THRESHOLDS = RANK_THRESHOLDS  # reference, not copy

# ── Inactivity penalty: days → escalones a bajar ─────────────────────────────
# 10d→1, 20d→3, 30d→5, 40d→7 ...  formula: steps = (days//10)*2 - 1, min 0
def _inactivity_steps(days: int) -> int:
    if days < 10:
        return 0
    return (days // 10) * 2 - 1


def _credits_after_penalty(current_credits: float, steps: int) -> float:
    """Return new credits after dropping `steps` rank levels.
    The player lands at threshold[rank_index - steps] + 1,
    or 0 if steps exceed their current rank index."""
    if steps <= 0:
        return current_credits
    # Find current rank index
    rank_idx = 0
    for i, t in enumerate(_PENALTY_THRESHOLDS):
        if current_credits >= t:
            rank_idx = i
    new_idx = max(0, rank_idx - steps)
    if new_idx == 0:
        return 0.0
    return float(_PENALTY_THRESHOLDS[new_idx] + 1)


def _set_rank_credits_lua(ranks: str, player_name: str, value: float) -> str:
    """Write credits for player_name in Foothold_Ranks.lua content string.
    Accepts both single and double quoted keys. Returns modified content."""
    start = ranks.find(f"['{player_name}']")
    if start == -1:
        start = ranks.find(f'["{player_name}"]')
    if start == -1:
        return ranks
    bs = ranks.find("{", start)
    if bs == -1:
        return ranks
    depth = 0
    for i in range(bs, len(ranks)):
        c = ranks[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                block     = ranks[bs:i + 1]
                lua_val   = str(int(value)) if float(value) == int(value) else str(value)
                new_block = re.sub(
                    r"(\[['\"']credits['\"']\]\s*=\s*)(-?\d+(?:\.\d+)?)",
                    rf"\g<1>{lua_val}", block, count=1
                )
                return ranks[:bs] + new_block + ranks[i + 1:]
    return ranks


def _set_campaign_points_lua(lua: str, player_name: str, value: float) -> str:
    """Write Points for player_name in Foothold campaign lua content string."""
    ps_start = lua.find("zonePersistance['playerStats']")
    if ps_start == -1:
        ps_start = lua.find('zonePersistance["playerStats"]')
    if ps_start == -1:
        return lua
    section = lua[ps_start:]
    p_start = section.find(f"['{player_name}']")
    if p_start == -1:
        p_start = section.find(f'["{player_name}"]')
    if p_start == -1:
        return lua
    bs = section.find("{", p_start)
    depth = 0
    for i in range(bs, len(section)):
        c = section[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                abs_start = ps_start + bs
                abs_end   = ps_start + i + 1
                block     = lua[abs_start:abs_end]
                lua_val   = str(int(value)) if float(value) == int(value) else str(value)
                new_block = re.sub(
                    r"(\[['\"']Points['\"']\]\s*=\s*)(-?\d+(?:\.\d+)?)",
                    rf"\g<1>{lua_val}", block, count=1
                )
                return lua[:abs_start] + new_block + lua[abs_end:]
    return lua

# ── Plugin class ──────────────────────────────────────────────────────────────

class FH_Report(Plugin):
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
        self._message_ids_file: str = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "message_ids.json"
        )


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
        self.updater.change_interval(seconds=interval)
        utils.safe_start(self.updater)
        # Start inactivity checker only if at least one server has it enabled
        any_penalty = any(
            v.get("inactivity_penalty") for k, v in raw.items()
            if isinstance(v, dict) and k != "DEFAULT"
        )
        if any_penalty:
            utils.safe_start(self.inactivity_checker)

    async def cog_unload(self) -> None:
        await utils.safe_cancel(self.inactivity_checker)
        await utils.safe_cancel(self.updater)
        await super().cog_unload()

    # ── Message IDs persistence (JSON file, no DB) ─────────────────────────

    def _load_message_ids(self) -> dict:
        if os.path.exists(self._message_ids_file):
            try:
                import json
                with open(self._message_ids_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except (ValueError, OSError):
                pass
        return {}

    def _save_message_ids(self) -> None:
        try:
            import json
            with open(self._message_ids_file, "w", encoding="utf-8") as f:
                json.dump(self._message_ids, f, indent=2)
        except OSError as e:
            self.log.error(f"FH_Report: could not save message IDs: {e}")

    # ── Core update task ──────────────────────────────────────────────────

    @tasks.loop(seconds=300)
    async def updater(self):
        import time
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
        default_cfg  = raw.get("DEFAULT") or {}

        # Warn about any fh_report.yaml server block whose key doesn't match
        # any currently-registered DCSServerBot instance name — a common
        # config mistake (e.g. copying the "DCS_Server" example verbatim
        # instead of the actual instance name from nodes.yaml) that
        # otherwise fails completely silently: the loop below just skips it
        # forever with no log trace at all, and the mismatch was previously
        # only ever surfaced by the /fh_report player command's own check.
        #
        # Gated by a grace period (not warned on first sight) because in a
        # large multi-node cluster, remote agent nodes can take a while to
        # register their servers with the central bot after startup — the
        # very first updater cycle can easily run before all of them have
        # checked in, which would otherwise log a permanent false-positive
        # warning (the one-shot version never re-checks) for an instance
        # that's actually fine seconds later. A key self-heals (its "first
        # seen unmatched" clock resets) the moment it matches again, so a
        # genuinely broken key still gets warned about, just not instantly.
        live_instance_names = {server.instance.name for server in self.bot.servers.values()}
        now_ts = datetime.now(timezone.utc).timestamp()
        configured_server_count = 0
        for cfg_key in raw.keys():
            if cfg_key == "DEFAULT":
                continue
            if cfg_key in live_instance_names:
                _unmatched_instance_since.pop(cfg_key, None)
                _unmatched_instance_warned.discard(cfg_key)
                if raw.get(cfg_key):
                    configured_server_count += 1
                continue
            first_seen = _unmatched_instance_since.setdefault(cfg_key, now_ts)
            if (now_ts - first_seen) < _UNMATCHED_INSTANCE_GRACE_SECONDS:
                continue
            if cfg_key not in _unmatched_instance_warned:
                _unmatched_instance_warned.add(cfg_key)
                self.log.warning(
                    f"FH_Report: server key '{cfg_key}' in fh_report.yaml doesn't match "
                    f"any configured DCSServerBot instance name — check the instance name "
                    f"in nodes.yaml. This server block will be skipped until fixed."
                )

        # Iterate all DCSSB servers — same pattern as Pretense.
        # Config is looked up by instance name (the key used in fh_report.yaml)
        # rather than server.name (the long DCS display name), so existing yaml
        # configs require no changes.
        stagger_seconds = _server_stagger_seconds(interval, configured_server_count)
        processed_server_count = 0
        for server in self.bot.servers.values():
            try:
                instance_name = server.instance.name
                srv_cfg = raw.get(instance_name)
                if not srv_cfg:
                    continue
                # Merge DEFAULT + instance overrides fresh each cycle (like Pretense)
                cfg = dict(default_cfg)
                cfg.update(srv_cfg)
                if processed_server_count > 0:
                    await asyncio.sleep(stagger_seconds)
                processed_server_count += 1
                await self._update_server(server, cfg)
            except Exception as e:
                self.log.error(
                    f"FH_Report [{server.instance.name}]: unexpected error: {e}", exc_info=True
                )

    @updater.before_loop
    async def before_updater(self):
        await self.bot.wait_until_ready()

    # ── Inactivity penalty task ───────────────────────────────────────────

    @tasks.loop(hours=6)
    async def inactivity_checker(self):
        """Check all configured servers for inactive pilots every 6 hours.
        Only runs if inactivity_penalty: 1 is set in fh_report.yaml."""
        raw         = self.locals or {}
        default_cfg = raw.get("DEFAULT") or {}
        for server in self.bot.servers.values():
            try:
                instance_name = server.instance.name
                srv_cfg = raw.get(instance_name)
                if not srv_cfg:
                    continue
                cfg = dict(default_cfg)
                cfg.update(srv_cfg)
                if not int(cfg.get("inactivity_penalty") or 0):
                    continue
                await self._run_inactivity_check(server, cfg)
            except Exception as e:
                self.log.error(
                    f"FH_Report [{server.instance.name}]: inactivity check error: {e}",
                    exc_info=True
                )

    @inactivity_checker.before_loop
    async def before_inactivity_checker(self):
        await self.bot.wait_until_ready()

    async def _run_inactivity_check(self, server, cfg: dict) -> None:
        """Apply inactivity credit penalties for one server instance.

        Penalty scale (days without connecting → rank levels dropped):
          10d → 1   20d → 3   30d → 5   40d → 7  ...  formula: (days//10)*2-1

        Credits are deducted so the player lands at threshold[rank-steps]+1.
        Campaign Points are only reduced when new_credits < current Points,
        and are reduced by the same delta (last points to be removed).

        State is persisted in saves_dir/.fhc/inactivity_penalties.json (UCID-keyed).
        All actions are logged to saves_dir/.fhc/inactivity_log.txt.
        """
        instance_name = server.instance.name
        node          = server.node
        saves_dir     = cfg.get("saves_dir")
        if not saves_dir:
            saves_dir = os.path.join(await server.get_missions_dir(), "Saves")

        # ── Load Foothold files ───────────────────────────────────────────
        persistence_file = await find_persistence_file(saves_dir, node)
        ranks_file       = os.path.join(saves_dir, "Foothold_Ranks.lua")
        try:
            ranks_data = (await node.read_file(ranks_file)).decode("utf-8")
        except FileNotFoundError:
            self.log.warning(f"FH_Report [{instance_name}]: Foothold_Ranks.lua not found, skipping inactivity check.")
            return
        camp_data = None
        if persistence_file:
            try:
                camp_data = (await node.read_file(persistence_file)).decode("utf-8")
            except FileNotFoundError:
                pass

        # ── Build ucid→name map from RankSave["ucidToName"] ─────────────
        ucid_to_name: dict[str, str] = {}
        for m in re.finditer(r"\[[\'\"]([a-f0-9]{32})[\'\"]\]=[\'\"]([^\'\"]+)[\'\"]", ranks_data):
            ucid_to_name[m.group(1)] = m.group(2)

        if not ucid_to_name:
            self.log.debug(f"FH_Report [{instance_name}]: no ucidToName entries found, skipping.")
            return

        # ── Load penalty state JSON ───────────────────────────────────────
        fhc_dir      = os.path.join(saves_dir, ".fhc")
        penalty_file = os.path.join(fhc_dir, "inactivity_penalties.json")
        log_file     = os.path.join(fhc_dir, "inactivity_log.txt")
        try:
            penalty_state = json.loads((await node.read_file(penalty_file)).decode("utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            penalty_state = {}

        today_str       = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        ranks_modified  = False
        camp_modified   = False
        log_lines: list[str] = []

        # ── Fetch last_seen for all UCIDs from DCSSB DB ───────────────────
        last_seen_map: dict[str, datetime | None] = {}
        try:
            async with self.apool.connection() as conn:
                for ucid in ucid_to_name:
                    async with conn.cursor() as cur:
                        await cur.execute(
                            "SELECT MAX(hop_off) FROM statistics WHERE player_ucid = %s",
                            (ucid,)
                        )
                        row = await cur.fetchone()
                        last_seen_map[ucid] = row[0] if row and row[0] else None
        except Exception as e:
            self.log.error(f"FH_Report [{instance_name}]: DB error fetching last_seen: {e}")
            return

        now_utc = datetime.now(timezone.utc)

        for ucid, player_name in ucid_to_name.items():
            last_seen = last_seen_map.get(ucid)
            if last_seen is None:
                continue
            # Ensure timezone-aware
            if last_seen.tzinfo is None:
                last_seen = last_seen.replace(tzinfo=timezone.utc)
            days_inactive = (now_utc - last_seen).days
            if days_inactive < 10:
                # Active player — reset their penalty state if any
                if ucid in penalty_state:
                    del penalty_state[ucid]
                continue

            steps_needed = _inactivity_steps(days_inactive)

            # Check if we already applied this level of penalty
            prev = penalty_state.get(ucid, {})
            prev_steps = prev.get("steps_applied", 0)
            if steps_needed <= prev_steps:
                # Update days but don't re-penalize
                penalty_state[ucid] = {
                    "name":           player_name,
                    "last_checked":   today_str,
                    "days_inactive":  days_inactive,
                    "steps_applied":  prev_steps,
                }
                continue

            # New penalty threshold crossed — apply the delta
            delta_steps = steps_needed - prev_steps

            # Get current credits from Foothold_Ranks.lua
            credit_m = None
            start = ranks_data.find(f"['{player_name}']")
            if start == -1:
                start = ranks_data.find(f'["{player_name}"]')
            if start != -1:
                bs = ranks_data.find("{", start)
                if bs != -1:
                    block_end = ranks_data.find("}", bs)
                    block = ranks_data[bs:block_end + 1]
                    credit_m = re.search(r"\[[\'\"]credits[\'\"]\]\s*=\s*([\d.]+)", block)

            if not credit_m:
                continue

            current_credits = float(credit_m.group(1))
            new_credits     = _credits_after_penalty(current_credits, steps_needed)

            if new_credits >= current_credits:
                continue  # Nothing to deduct

            # ── Write Foothold_Ranks.lua ──────────────────────────────────
            ranks_data     = _set_rank_credits_lua(ranks_data, player_name, new_credits)
            ranks_modified = True

            # ── Deduct campaign Points if needed ─────────────────────────
            camp_points_deducted = 0.0
            if camp_data and new_credits < current_credits:
                # Get current campaign Points for this player
                pts_m = None
                ps_start = camp_data.find("zonePersistance['playerStats']")
                if ps_start == -1:
                    ps_start = camp_data.find('zonePersistance["playerStats"]')
                if ps_start != -1:
                    section = camp_data[ps_start:]
                    p_start = section.find(f"['{player_name}']")
                    if p_start == -1:
                        p_start = section.find(f'["{player_name}"]')
                    if p_start != -1:
                        bs2 = section.find("{", p_start)
                        be2 = section.find("}", bs2)
                        block2 = section[bs2:be2 + 1]
                        pts_m = re.search(r"\[[\'\"]Points[\'\"]\]\s*=\s*([\d.]+)", block2)

                if pts_m:
                    current_points = float(pts_m.group(1))
                    # Only touch Points if new_credits < current Points
                    if new_credits < current_points:
                        delta          = current_credits - new_credits
                        new_points     = max(0.0, current_points - delta)
                        camp_data      = _set_campaign_points_lua(camp_data, player_name, new_points)
                        camp_modified  = True
                        camp_points_deducted = current_points - new_points

            # ── Update penalty state ──────────────────────────────────────
            penalty_state[ucid] = {
                "name":          player_name,
                "last_checked":  today_str,
                "days_inactive": days_inactive,
                "steps_applied": steps_needed,
            }

            # ── Build log line ────────────────────────────────────────────
            ts  = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            log = (
                f"{ts} | {ucid} | {player_name} | "
                f"{days_inactive} days inactive | -{delta_steps} rank level(s) | "
                f"{int(current_credits):,} → {int(new_credits):,} credits"
            )
            if camp_points_deducted > 0:
                log += f" | campaign Points -{int(camp_points_deducted):,}"
            log_lines.append(log)
            self.log.info(f"FH_Report [{instance_name}]: inactivity penalty: {log}")

        # ── Write modified Lua files back to node ─────────────────────────
        if ranks_modified:
            await write_bytes_to_node(node, ranks_file, ranks_data.encode("utf-8"), log=self.log)

        if camp_modified and persistence_file:
            await write_bytes_to_node(node, persistence_file, camp_data.encode("utf-8"), log=self.log)

        # ── Write penalty state JSON ──────────────────────────────────────
        await write_bytes_to_node(
            node, penalty_file,
            json.dumps(penalty_state, indent=2, ensure_ascii=False).encode("utf-8"),
            log=self.log
        )

        # ── Append to log file ────────────────────────────────────────────
        if log_lines:
            try:
                existing = b""
                try:
                    existing = await node.read_file(log_file)
                except FileNotFoundError:
                    pass
                new_content = existing + "\n".join(log_lines).encode("utf-8") + b"\n"
            except Exception as e:
                self.log.error(f"FH_Report [{instance_name}]: failed to read inactivity log: {e}")
            else:
                await write_bytes_to_node(node, log_file, new_content, log=self.log)

    def _resolve_report_layout(self, server_name: str, cfg: dict) -> tuple[str, dict]:
        """Resolve (report_layout, points_detail) straight from config.

        report_layout supports comma-separated rotation: "DP, SR" shows
        "DP" one cycle, "SR" the next, back to "DP" after that, advancing
        exactly one step per update_interval — no separate cadence
        setting, no persistence across bot restarts (always starts back
        at the first group on load). Any number of groups is allowed. A
        single value (no comma) behaves exactly as before, with nothing
        to rotate. points_detail_D/S/R are NOT part of the rotation —
        they're global and apply the same regardless of which group is
        showing this cycle.

        No translation happens here any more — migrate_config.py converts
        any legacy points_order/compact_points (and the older shared
        single-string points_detail format) into report_layout and the
        three independent points_detail_D/points_detail_S/points_detail_R
        keys once, permanently, in the YAML itself. points_detail is
        returned as a dict {"D": "...", "S": "...", "R": "..."} — a role
        with no points_detail_<role> key at all is simply absent from the
        dict, and _render_layout_tables treats that as "own value only",
        same as every other show_*-style feature in this plugin defaulting
        to off. Nothing here forces a table's own letter into its string —
        that has to be written explicitly if wanted.
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
        """Return path to daily_snapshot.json cache file."""
        return os.path.join(saves_dir, ".fhc", "daily_snapshot.json")

    def _get_history_file(self, saves_dir: str) -> str:
        """Return path to daily_history.json — the Podium feature's historical
        record of each day's top-10, keyed by date (YYYY-MM-DD)."""
        return os.path.join(saves_dir, ".fhc", "daily_history.json")

    def _load_daily_history(self, saves_dir: str) -> dict:
        """Load daily history from disk. Returns {date_str: [event, ...]},
        where each event is {"campaign_restart": bool, "top": [{"name","points"}, ...]}.
        A date can have more than one event if a campaign restart happened
        on the same calendar day as the normal daily rollover — both are
        kept, never overwritten."""
        path = self._get_history_file(saves_dir)
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    return json.load(f)
            except (ValueError, OSError):
                pass
        return {}

    async def _save_daily_history(self, saves_dir: str, data: dict, node) -> None:
        """Save daily history to disk atomically, via write_bytes_to_node
        (works for both local and remote-node instances, with the old
        local-only fallback and update hint for pre-3.0.4.28 DCSServerBot).
        Ensures the .fhc subdirectory exists first — safe to create locally
        here specifically, since saves_dir itself is already known-reachable
        (we've successfully read other files from it earlier this same
        cycle), unlike Foothold's own save files where a missing directory
        is itself the signal that we're on an unreachable remote path."""
        path = self._get_history_file(saves_dir)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # Written most-recent-date-first purely for readability when someone
        # opens the file by hand — _build_podium_table always re-sorts dates
        # itself when reading, so this ordering has zero effect on what's
        # actually displayed.
        sorted_data = dict(sorted(data.items(), key=lambda kv: kv[0], reverse=True))
        await write_bytes_to_node(
            node, path,
            json.dumps(sorted_data, indent=2).encode("utf-8"),
            log=self.log
        )

    def _load_daily_snapshot(self, saves_dir: str) -> dict:
        """Load daily snapshot from disk. Returns dict with keys:
        'date' (YYYY-MM-DD), 'snapshot' {name: pts}, 'stats_snapshot' {name: {stat: val}}."""
        path = self._get_daily_file(saves_dir)
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    return json.load(f)
            except (ValueError, OSError):
                pass
        return {}

    async def _save_daily_snapshot(self, saves_dir: str, data: dict, node) -> None:
        """Save daily snapshot to disk atomically, via write_bytes_to_node
        (works for both local and remote-node instances, with the old
        local-only fallback and update hint for pre-3.0.4.28 DCSServerBot).
        Ensures the .fhc subdirectory exists first — see _save_daily_history
        for why this is safe to do locally specifically for this subfolder."""
        path = self._get_daily_file(saves_dir)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        await write_bytes_to_node(
            node, path,
            json.dumps(data, indent=2).encode("utf-8"),
            log=self.log
        )

    async def _compute_daily_points(self, saves_dir: str, campaign_stats: dict,
                              session_stats_raw: dict, reset_hour: int, node,
                              persistence_filename: str | None = None,
                              name_to_ucid: dict | None = None) -> tuple[dict, dict, bool]:
        """Compute today's points and today's combat stats for each player by
        comparing current campaign values against the snapshot taken at reset_hour UTC.
        Returns (daily_pts, daily_stats, campaign_restarted):
          daily_pts   = {name: daily_points}      — only players with daily_pts > 0
          daily_stats = {name: {stat_key: delta}} — used for the daily card (show_daily_card)

        Manual reset: this plugin has no commands. To manually reset the daily
        counters, delete saves_dir/.fhc/daily_snapshot.json — a missing snapshot
        is always treated as a fresh baseline (current values), so the daily
        counter restarts at 0 rather than retroactively counting everything
        accumulated up to that point.

        Mid-campaign callsign-change reconciliation: Foothold's playerStats
        is keyed by in-game name, not UCID — so a player who changes callsign
        mid-campaign gets a BRAND NEW playerStats entry under the new name,
        with no history at all under it. Left unhandled, this would look
        exactly like a new player joining, and their entire accumulated
        total under the new name would be misattributed as "today's gain"
        the moment it first appears (name_to_ucid, built from the already-
        parsed Foothold_Ranks.lua data, is used to detect this: if a name
        that's new to our snapshot shares a UCID with a name we already had
        tracked, it's a rename, not a new player, and today's already-earned
        total is carried across to the new name automatically).

        Mission/map reset handling: a mid-day mission or map change (a new
        Foothold save file, or the same file wiped in place) must NOT
        interrupt today's point/stat counting, and is never treated as a
        Podium-worthy closing event — only the real scheduled reset_hour
        rollover closes a day and feeds Podium. Detected via any of:
          (a) the active persistence file's name changed since last cycle
          (b) campaign_stats/session_stats_raw lost every player that was
              in the snapshot (the new file started empty)
          (c) the existing points+kills-both-dropped heuristic (an in-place
              admin/Foothold reset without a filename change)
        When any of these fire (and it's not also a real calendar-day
        reset), today's already-earned points/stats are preserved in a
        separate 'carry_over' bucket, and the snapshot rebases to the new
        file's own (usually empty) starting values — so
        daily = (current - snapshot) + carry_over continues seamlessly
        across the swap, with no visible interruption and no Podium entry.
        """
        now_utc   = datetime.now(timezone.utc)
        today_str = now_utc.strftime("%Y-%m-%d")

        snap                = self._load_daily_snapshot(saves_dir)
        snap_date           = snap.get("date", "")
        snapshot            = snap.get("snapshot", {})
        stats_snapshot      = snap.get("stats_snapshot", {})
        last_daily_saved    = snap.get("last_daily", {})
        last_daily_stats_saved = snap.get("last_daily_stats", {})
        carry_over          = snap.get("carry_over", {})
        stats_carry_over    = snap.get("stats_carry_over", {})
        last_persistence_fn = snap.get("persistence_filename", "")
        name_to_ucid_snapshot = snap.get("name_to_ucid", {})
        # Stat categories ever seen across ALL cycles, not just the current
        # in-memory stats_snapshot — a plain set(list) accumulator that only
        # grows. Deriving "trusted" categories solely from stats_snapshot
        # broke on a mid-day mission/map change: that branch rebases
        # stats_snapshot to the new file's session_stats_raw, which is
        # legitimately empty right after the swap (nobody's flown yet),
        # so EVERY category got silently dropped from the Daily card for
        # the rest of that day (Session wasn't affected — it doesn't use
        # this gate). Persisting the set survives that empty moment.
        known_stat_keys = set(snap.get("known_stat_keys", []))

        # ── Mid-campaign callsign-change reconciliation ─────────────────────
        # See docstring above. Runs BEFORE mission-reset detection since a
        # single rename shouldn't be confused with one (other players' names
        # still overlap fine with the snapshot in that case).
        if name_to_ucid and name_to_ucid_snapshot:
            ucid_to_old_name = {u: n for n, u in name_to_ucid_snapshot.items() if u}
            for new_name in list(campaign_stats.keys()):
                if new_name in snapshot:
                    continue  # already tracked under this exact name
                new_ucid = name_to_ucid.get(new_name)
                if not new_ucid:
                    continue
                old_name = ucid_to_old_name.get(new_ucid)
                if not old_name or old_name == new_name or old_name in campaign_stats:
                    # No match, no-op rename, or the "old" name is still
                    # present in playerStats too (so it's genuinely a
                    # different, unrelated player, not a rename) — skip.
                    continue
                migrated_pts = last_daily_saved.get(old_name, 0)
                if migrated_pts > 0:
                    carry_over[new_name] = carry_over.get(new_name, 0) + migrated_pts
                migrated_stats = last_daily_stats_saved.get(old_name, {})
                if migrated_stats:
                    merged = dict(stats_carry_over.get(new_name, {}))
                    for key, val in migrated_stats.items():
                        if val > merged.get(key, 0):
                            merged[key] = val
                    stats_carry_over[new_name] = merged
                snapshot[new_name] = campaign_stats[new_name]
                if new_name in session_stats_raw:
                    stats_snapshot[new_name] = dict(session_stats_raw[new_name])
                self.log.info(
                    f"FH_Report: detected a mid-campaign callsign change for "
                    f"{saves_dir}: '{old_name}' -> '{new_name}' (same UCID) — "
                    f"today's totals carried over to the new name."
                )

        # ── Mid-day mission/map reset detection (never a Podium event) ─────
        filename_changed = bool(last_persistence_fn) and bool(persistence_filename) and \
                            last_persistence_fn != persistence_filename
        common_with_snapshot = set(snapshot) & set(campaign_stats)
        data_vanished = bool(snapshot) and not common_with_snapshot

        # ── Campaign restart detection (in-place reset, same file) ─────────
        # Points and kill counts only ever increase during a normal campaign.
        # If both the total points AND total kills (Air + Ground Units) for
        # players common to both the snapshot and current data have dropped
        # significantly, treat this the same as the other mission-reset
        # signals above — never a Podium event, daily counts carry over.
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
        reset_time     = now_utc.replace(hour=reset_hour, minute=0, second=0, microsecond=0)
        first_run      = not snap_date
        date_reset_due = (not first_run) and snap_date != today_str and now_utc >= reset_time

        if first_run:
            # No prior day to close or carry over — start completely fresh.
            snapshot         = dict(campaign_stats)
            stats_snapshot   = {name: dict(stats) for name, stats in session_stats_raw.items()}
            carry_over       = {}
            stats_carry_over = {}

        elif date_reset_due:
            reason = " (a mid-day mission/map reset was also detected and is folded in)" if mission_reset else ""
            self.log.debug(f"FH_Report: daily reset for {saves_dir} at {reset_hour:02d}:00 UTC{reason}")

            # Close out the day for Podium — using the CURRENT snapshot/
            # carry_over (which already correctly reflect any mid-day
            # mission swaps folded in via the branch below on prior
            # cycles), so this is accurate even if the day had several
            # mission changes in it. Falls back to last_daily_saved for
            # anyone not resolvable via the fresh delta (covers the rare
            # edge case of a mission swap landing in this exact same cycle
            # as the date-based reset, before campaign_stats/snapshot could
            # be reconciled for it).
            closing_daily = {}
            for name, current_pts in campaign_stats.items():
                delta = max(0, current_pts - snapshot.get(name, 0)) + carry_over.get(name, 0)
                if delta > 0:
                    closing_daily[name] = delta
            for name, carried in carry_over.items():
                if name not in closing_daily and carried > 0:
                    closing_daily[name] = carried
            for name, val in last_daily_saved.items():
                if name not in closing_daily and val > 0:
                    closing_daily[name] = val

            if closing_daily:
                top_list = sorted(closing_daily.items(), key=lambda kv: kv[1], reverse=True)[:50]
                history    = self._load_daily_history(saves_dir)
                history.setdefault(snap_date, []).append({
                    "campaign_restart": False,
                    "top": [{"name": n, "points": p} for n, p in top_list],
                })
                await self._save_daily_history(saves_dir, history, node)

            snapshot         = dict(campaign_stats)
            stats_snapshot   = {name: dict(stats) for name, stats in session_stats_raw.items()}
            carry_over       = {}
            stats_carry_over = {}

        elif mission_reset:
            # Mid-day mission/map change — never a Podium event. By this
            # point campaign_stats/session_stats_raw already belong to the
            # NEW file (often empty), so we can no longer compute "how much
            # was earned today" via current-minus-old-snapshot — the only
            # reliable source left is last_daily_saved/last_daily_stats_saved,
            # persisted every cycle for exactly this reason. Fold that into
            # carry_over, then rebase the snapshot to the new file so
            # (current - new_snapshot) + carry_over continues the day
            # seamlessly with no visible interruption.
            self.log.debug(
                f"FH_Report: mid-day mission/map reset detected for {saves_dir} "
                f"(filename_changed={filename_changed}, data_vanished={data_vanished}, "
                f"campaign_restarted={campaign_restarted}) — carrying today's totals "
                f"forward, no Podium entry."
            )
            new_carry_over = dict(carry_over)
            for name, val in last_daily_saved.items():
                if val > 0:
                    new_carry_over[name] = max(new_carry_over.get(name, 0), val)
            carry_over = new_carry_over

            new_stats_carry_over = {name: dict(stats) for name, stats in stats_carry_over.items()}
            for name, stats in last_daily_stats_saved.items():
                merged = dict(new_stats_carry_over.get(name, {}))
                for key, val in stats.items():
                    if val > merged.get(key, 0):
                        merged[key] = val
                if merged:
                    new_stats_carry_over[name] = merged
            stats_carry_over = new_stats_carry_over

            snapshot       = dict(campaign_stats)
            stats_snapshot = {name: dict(stats) for name, stats in session_stats_raw.items()}

        # ── Calculate today's point delta for each player ──────────────────
        # (snapshot/carry_over above already reflect any resets/carries
        # that happened this cycle, so this is a single, uniform formula.)
        daily = {}
        for name, current_pts in campaign_stats.items():
            delta = max(0, current_pts - snapshot.get(name, 0)) + carry_over.get(name, 0)
            if delta > 0:
                daily[name] = delta
        for name, carried in carry_over.items():
            if name not in daily and carried > 0:
                daily[name] = carried

        # ── Calculate today's combat stats delta for each player ───────────
        # A stat key is only trustworthy for a delta if it was already being
        # tracked as of the last snapshot (i.e. present for at least one
        # player in stats_snapshot). If a key appears nowhere in the old
        # snapshot, the system simply wasn't recording it yet at the last
        # reset — computing current_val - 0 would show today's entire
        # cumulative value mislabeled as "today's activity" (this happened
        # with "Points spent" right after it was added to the raw stats).
        # Skip such keys for today only; the next snapshot (taken at the
        # following reset) will include them naturally since it's built
        # directly from session_stats_raw, so deltas resume correctly from
        # the next reset onward.
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

        # Grow the persisted "ever seen" set with whatever categories are
        # visible this cycle, so they're trusted from the NEXT cycle on
        # (mirrors the original one-day-grace-period intent, just anchored
        # to a persistent accumulator instead of the transient in-memory
        # stats_snapshot).
        for _stats in session_stats_raw.values():
            known_stat_keys.update(_stats.keys())
        for _stats in stats_snapshot.values():
            known_stat_keys.update(_stats.keys())

        # Persist the snapshot every cycle (not just on reset), always
        # including 'last_daily' and the carry-over buckets, plus the
        # current persistence filename (used to detect the next mission
        # change) and reset markers.
        await self._save_daily_snapshot(saves_dir, {
            "date":                 today_str if (first_run or date_reset_due) else (snap_date or today_str),
            "snapshot":             snapshot,
            "stats_snapshot":       stats_snapshot,
            "last_daily":           daily,
            "last_daily_stats":     daily_stats,
            "carry_over":           carry_over,
            "stats_carry_over":     stats_carry_over,
            "persistence_filename": persistence_filename or last_persistence_fn,
            "name_to_ucid":         {**name_to_ucid_snapshot, **(name_to_ucid or {})},
            "known_stat_keys":      sorted(known_stat_keys),
        }, node)

        return daily, daily_stats, campaign_restarted

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
            self.log.debug(f"FH_Report: punishment points not available: {e}")
            return {}

    async def _update_server(self, server, cfg: dict):
        """Update the Discord embed for one server instance.
        server  — DCSSB Server object (provides server.node.read_file())
        cfg     — merged config dict (DEFAULT + instance overrides)
        Mirrors the Pretense pattern: read files via server.node.read_file()
        so the Master transparently fetches data from remote agent nodes."""

        instance_name = server.instance.name

        if _bool_cfg(cfg.get("disable_updates")):
            # This instance is intentionally silenced — typically because a
            # duplicate fh_report installation exists elsewhere in the same
            # cluster (e.g. one config per agent box) pointing at the same
            # channel. Skip entirely: no read, no post, no edit.
            return

        channel_id    = cfg.get("channel_id")
        if not channel_id:
            self.log.warning(f"FH_Report [{instance_name}]: channel_id not configured.")
            return

        channel = self.bot.get_channel(int(channel_id))
        if not channel:
            self.log.warning(f"FH_Report [{instance_name}]: channel {channel_id} not found.")
            return

        # Resolve saves_dir — prefer explicit config, fall back to get_missions_dir()
        # exactly as Pretense does: os.path.join(await server.get_missions_dir(), 'Saves')
        saves_dir = cfg.get("saves_dir")
        if not saves_dir:
            saves_dir = os.path.join(await server.get_missions_dir(), "Saves")

        source_node = server.node
        node = _UpdateReadCache(source_node)

        # One-time-per-instance write self-test — see run_write_self_test()
        # for why this doesn't wait for a real correction to be needed.
        await run_write_self_test(source_node, saves_dir, log=self.log)

        persistence_file = await find_persistence_file(saves_dir, node)
        if not persistence_file:
            self.log.warning(f"FH_Report [{instance_name}]: no foothold_*.lua found in {saves_dir}")
            return

        ranks_file    = os.path.join(saves_dir, "Foothold_Ranks.lua")
        ranks_missing = False
        ranks_source  = None
        try:
            ranks_source = await node.read_file(ranks_file)
        except FileNotFoundError:
            self.log.debug(
                f"FH_Report [{instance_name}]: Foothold_Ranks.lua not found — "
                f"showing zone status without leaderboard."
            )
            ranks_missing = True

        if not ranks_missing:
            # Deduplicate player entries caused by callsign changes before parsing.
            try:
                if await deduplicate_ranks(ranks_file, persistence_file, source_node, ranks_source):
                    node.invalidate(ranks_file)
            except Exception as e:
                self.log.error(f"FH_Report [{instance_name}]: deduplication error: {e}")

        try:
            excluded_ucids = cfg.get("excluded_ucids") or []
            zones          = await parse_zones(persistence_file, node)
            players        = {} if ranks_missing else await parse_ranks(ranks_file, excluded_ucids, node)
            campaign_stats, session_stats_raw, name_to_ucid_native = await parse_player_stats(persistence_file, node)
        except Exception as e:
            self.log.error(f"FH_Report [{instance_name}]: error parsing data: {e}")
            return

        # Optional private hook — post-processes players dict
        if _HAS_HOOK:
            try:
                players = _fh_hook.post_process(players, cfg, instance_name, campaign_stats)
            except Exception:
                pass

        show_punishment   = _bool_cfg(cfg.get("show_punishment"))
        punishment_points = {}
        if show_punishment:
            punishment_points = await self._fetch_punishment_points()

        # Compute daily points first so we know if daily data exists before
        # rendering. Needed whenever "D" is one of the tables, or "D" is
        # requested as extra detail on some OTHER table via its own
        # points_detail_<role>, or "P" is present — Podium's own
        # daily_history capture-on-reset logic lives inside
        # _compute_daily_points, so it must run even for a Podium-only
        # layout or the history file would never get populated at all.
        # Intentionally checked against the RAW (un-split) report_layout
        # string, commas included: with rotation ("DP, SR"), daily/Podium
        # data is kept warm on every cycle regardless of which single
        # group is actually showing this time, so nothing goes stale or
        # has to be rebuilt in a rush the moment its group comes back up.
        raw_layout = str(cfg.get("report_layout") or "R").strip().upper()
        needs_daily = ("D" in raw_layout) or ("P" in raw_layout) or any(
            "D" in str(cfg.get(f"points_detail_{role}") or "").upper()
            for role in ("D", "S", "R")
        )
        # Also run daily-points computation (and its campaign-restart
        # detection) whenever waypoint sorting is enabled, regardless of
        # report_layout — that's the signal used to know when to refresh
        # the shared waypoint cache (see sort_zones_by_waypoint below).
        needs_daily = needs_daily or _bool_cfg(cfg.get("sort_zones_by_waypoint"))
        daily_pts: dict = {}
        daily_stats: dict = {}
        campaign_restarted_now = False
        # NOTE: intentionally does NOT require `campaign_stats` to be
        # non-empty. Right after a mission/map change, the new file's
        # playerStats is legitimately empty until someone scores — but
        # that is exactly when this must still run: it's what detects the
        # persistence-filename change and migrates today's already-earned
        # points into carry_over (see _compute_daily_points' docstring).
        # Skipping this call on an empty campaign_stats silently freezes
        # daily_snapshot.json at the previous mission's state forever.
        if needs_daily:
            reset_hour    = int(cfg.get("daily_reset_hour") or 0)
            # Override with day-specific hour if daily_reset_schedule is defined
            schedule      = cfg.get("daily_reset_schedule") or {}
            if schedule:
                day_keys  = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
                today_key = day_keys[datetime.now(timezone.utc).weekday()]
                if today_key in schedule:
                    reset_hour = int(schedule[today_key])
            # Prefer the native ucidToName from THIS SAME file (Foothold
            # 4.9.1+ — confirmed with Leka, lives right alongside
            # playerStats). Falls back to cross-referencing the already-
            # parsed Foothold_Ranks.lua data (older Foothold versions that
            # don't have this field in the mission progress file yet).
            name_to_ucid_fallback = {n: d.get("ucid") for n, d in players.items() if d.get("ucid")}
            # Merge per-player, not "pick one source entirely" — during a
            # transition period right after updating Foothold to 4.9.1+,
            # players already in playerStats from BEFORE the update may not
            # yet have an entry in the native ucidToName table (only
            # populated for them once they reconnect). Native takes
            # priority per-name where available; the older Ranks-based
            # cross-reference still fills in anyone the native table
            # doesn't have yet, instead of being discarded wholesale.
            name_to_ucid = {**name_to_ucid_fallback, **name_to_ucid_native}
            daily_pts, daily_stats, campaign_restarted_now = await self._compute_daily_points(saves_dir, campaign_stats, session_stats_raw, reset_hour, source_node, os.path.basename(persistence_file) if persistence_file else None, name_to_ucid)

        # Detect if session data exists (any player with session_points > 0)
        has_session = any(d.get("session_points", 0) > 0 for d in players.values())

        # Load daily_history once here (cheap, tiny file) so build_embed can
        # reuse it below without reading the file twice. If there's no
        # history yet, _render_layout_tables' own Podium step simply
        # renders nothing for "P" — no separate skip-check needed here.
        daily_history_data = self._load_daily_history(saves_dir)

        current_layout, current_detail = self._resolve_report_layout(instance_name, cfg)

        # Zone ordering by waypoint number — opt-in, uses hot injection to
        # dump Foothold's in-memory WaypointList (never persisted to any
        # save file) to a shared cache file also used by FH_Control. Only
        # re-triggered when the cache is missing entirely, or when a
        # campaign restart was just detected (a new mission/map load is
        # exactly the event that would change zone-to-waypoint assignments).
        # Never re-triggered on every ordinary cycle, since WaypointList is
        # static for the lifetime of a stable campaign.
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
                    self.log.warning(f"FH_Report [{instance_name}]: waypoint hot-write failed: {e}")
            try:
                waypoint_map = await load_waypoint_list(saves_dir, node)
            except Exception as e:
                self.log.warning(f"FH_Report [{instance_name}]: could not load waypoint cache: {e}")
                waypoint_map = {}

        # Player command hint — plain-text reminder of /fh_report player,
        # shown as a second line in the footer. Active by default (migrate
        # inserts it explicitly into DEFAULT) so users discover the command
        # without the admin having to opt in.
        player_cmd_hint = None
        raw_hint_flag   = cfg.get("show_player_cmd_hint")
        show_hint       = _bool_cfg(raw_hint_flag) if raw_hint_flag is not None else True
        if show_hint:
            player_cmd_hint = str(cfg.get("player_cmd_hint_text")
                                  or "Type /fh_report player to see your own stats.")

        embed = build_embed(
            zones               = zones,
            players             = players,
            campaign_name       = cfg.get("campaign_name", "Foothold Campaign"),
            max_zones           = cfg.get("max_zones") or None,
            max_pilots          = cfg.get("max_pilots") or None,
            bar_length          = int(cfg.get("bar_length") or 40),
            slot_status         = _bool_cfg(cfg.get("slot_status")),
            punishment_points   = punishment_points,
            show_punishment     = show_punishment,
            show_all_pilots     = _bool_cfg(cfg.get("show_all_pilots")),
            strip_callsign_flag = _bool_cfg(cfg.get("strip_callsign")),
            zone_name_length    = max(8, min(24, int(cfg.get("zone_name_length") or 16))),
            max_pilots_2t       = cfg.get("max_pilots_2t") or None,
            campaign_stats      = campaign_stats,
            report_layout       = current_layout,
            points_detail       = current_detail,
            bar_style_emoji     = _bool_cfg(cfg.get("bar_style_emoji")),
            daily_points        = daily_pts,
            max_pilots_3t       = int(cfg.get("max_pilots_3t") or 0) or None,
            show_pilot_card     = _bool_cfg(cfg.get("show_pilot_card")),
            pilot_card_icon     = str(cfg.get("pilot_card_icon") or "🔸"),
            show_session_card   = _bool_cfg(cfg.get("show_session_card")),
            session_card_icon   = str(cfg.get("session_card_icon") or "🔸"),
            session_stats_raw   = session_stats_raw,
            show_daily_card     = _bool_cfg(cfg.get("show_daily_card")),
            daily_card_icon     = str(cfg.get("daily_card_icon") or "🔸"),
            daily_stats_raw     = daily_stats,
            player_cmd_hint     = player_cmd_hint,
            daily_history       = daily_history_data if "P" in current_layout else None,
            podium_days         = int(cfg.get("podium_days") if cfg.get("podium_days") is not None else 7),
            podium_top          = max(1, min(50, int(cfg.get("podium_top") or 1))),
            podium_combined_days      = int(cfg.get("podium_combined_days") if cfg.get("podium_combined_days") is not None else 7),
            podium_combined_top       = max(1, min(50, int(cfg.get("podium_combined_top") or 1))),
            podium_combined_min3_latest_day = _bool_cfg(cfg.get("podium_combined_min3_latest_day")),
            sort_zones_by_waypoint = sort_zones_by_wp,
            waypoint_map        = waypoint_map,
        )

        try:
            msg_id = self._message_ids.get(instance_name)
            msg = None
            if msg_id:
                try:
                    msg = await channel.fetch_message(msg_id)
                except discord.NotFound:
                    self.log.warning(f"FH_Report [{instance_name}]: previous message not found, searching channel for an existing one.")
                    self._message_ids.pop(instance_name, None)

            if msg is None:
                # No known message (lost message_ids.json entry, or first run
                # on this instance). Before creating a new one, check if a
                # matching FH_Report message already exists in this channel —
                # this makes duplicate posts structurally impossible even if
                # multiple fh_report installations end up pointing at the
                # same channel_id (e.g. one config per agent box).
                campaign_name  = cfg.get("campaign_name", "Foothold Campaign")
                expected_title = f"📡  {campaign_name}"
                async for hist_msg in channel.history(limit=50):
                    if (hist_msg.author.id == self.bot.user.id and hist_msg.embeds
                            and hist_msg.embeds[0].title == expected_title):
                        msg = hist_msg
                        self._message_ids[instance_name] = msg.id
                        self._save_message_ids()
                        self.log.info(
                            f"FH_Report [{instance_name}]: adopted existing message "
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
            self.log.error(f"FH_Report [{instance_name}]: Discord error: {e}")

    # ── /fh_report player ────────────────────────────────────────────────

    def _configured_instances(self) -> list[str]:
        """Return instance-name keys configured in fh_report.yaml (excludes DEFAULT)."""
        raw = self.locals or {}
        return [k for k in raw.keys() if k != "DEFAULT"]

    def _get_server_by_instance(self, instance_name: str):
        """Find the DCSSB Server object matching a configured instance name."""
        for server in self.bot.servers.values():
            if server.instance.name == instance_name:
                return server
        return None

    def _merged_cfg(self, instance_name: str) -> dict:
        raw = self.locals or {}
        cfg = dict(raw.get("DEFAULT") or {})
        cfg.update(raw.get(instance_name) or {})
        return cfg

    def _resolve_server_from_channel(self, interaction: discord.Interaction) -> str | None:
        """Auto-detect which configured instance owns the channel the command
        was invoked in, by matching interaction.channel_id against each
        instance's configured channel_id. Falls back to the single configured
        instance if there's only one. Returns None if ambiguous/not found."""
        configured = self._configured_instances()
        for instance_name in configured:
            cfg = self._merged_cfg(instance_name)
            if str(cfg.get("channel_id") or "") == str(interaction.channel_id):
                return instance_name
        if len(configured) == 1:
            return configured[0]
        return None

    def _is_admin(self, interaction: discord.Interaction, server_name: str) -> bool:
        """True if the calling user matches any entry in the 'admin' config —
        a comma-separated string where each entry may be a Discord role name
        (as defined in DCSSB) or a specific username. Defaults to 'Admin' if
        not configured. Kept tolerant of a legacy list value (old yaml files
        from before admin became a comma-separated string)."""
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

    async def _autocomplete_report_player(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        server_name = self._resolve_server_from_channel(interaction)
        if not server_name:
            return []
        # Non-admins never get name suggestions — they can only query themselves,
        # which doesn't need the player_name parameter at all.
        if not self._is_admin(interaction, server_name):
            return []

        # Primary path: query DCSServerBot's own `players` table (ucid +
        # name — the same table /linkme and our self-lookup path already
        # use) instead of re-reading and re-parsing Foothold_Ranks.lua on
        # every keystroke. Much cheaper against a roster this size, and it
        # hands back a UCID rather than a raw name, so the actual command
        # below can match it against the Foothold data by UCID — sidestepping
        # callsign-prefix/strip_callsign mismatches entirely for this path.
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

        srv = self._get_server_by_instance(server_name)
        if srv is None:
            return []
        cfg       = self._merged_cfg(server_name)
        saves_dir = cfg.get("saves_dir")
        if not saves_dir:
            try:
                saves_dir = os.path.join(await srv.get_missions_dir(), "Saves")
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
        player_name="Player name — admin only. Leave empty to see your own stats."
    )
    @app_commands.autocomplete(player_name=_autocomplete_report_player)
    async def player(self, interaction: discord.Interaction,
                     player_name: str | None = None):
        ephemeral = utils.get_ephemeral(interaction)
        await interaction.response.defer(ephemeral=ephemeral)

        # Auto-detect the server from the channel this command was run in —
        # each configured instance posts its embed to a specific channel_id.
        server = self._resolve_server_from_channel(interaction)
        if not server:
            await interaction.followup.send(
                "❌ Couldn't determine which server this channel belongs to. "
                "Run this command in the channel where FH_Report posts the campaign embed.",
                ephemeral=True)
            return

        srv = self._get_server_by_instance(server)
        if srv is None:
            await interaction.followup.send(
                f"❌ Server **`{server}`** not found among configured DCSServerBot instances.",
                ephemeral=True)
            return

        is_admin = self._is_admin(interaction, server)
        if player_name and not is_admin:
            await interaction.followup.send(
                "❌ You can only view your own stats. Leave the `player_name` field empty.",
                ephemeral=True)
            return

        cfg       = self._merged_cfg(server)
        saves_dir = cfg.get("saves_dir")
        if not saves_dir:
            saves_dir = os.path.join(await srv.get_missions_dir(), "Saves")
        node = srv.node

        try:
            # No longer forcing bc:saveToDisk() here — per @leka1986: Foothold
            # already autosaves every 60s on its own, and a forced save is a
            # genuinely heavy write (all AI state, loadouts, positions, the
            # director state — thousands of lines), not a cheap one. Forcing
            # it on every /fh_report player call cost real server resources
            # for at most 60s of extra freshness — not worth it. We just read
            # whatever Foothold's own autosave cycle already wrote.
            persistence_file = await find_persistence_file(saves_dir, node)
            if not persistence_file:
                await interaction.followup.send(
                    f"❌ No Foothold save file found for **`{server}`**.", ephemeral=True)
                return
            ranks_file = os.path.join(saves_dir, "Foothold_Ranks.lua")

            excluded_ucids = cfg.get("excluded_ucids") or []
            players        = await parse_ranks(ranks_file, excluded_ucids, node)
            campaign_stats, session_stats_raw, name_to_ucid_native = await parse_player_stats(persistence_file, node)
        except Exception as e:
            await interaction.followup.send(f"❌ Error reading campaign files:\n```{e}```", ephemeral=True)
            return

        if player_name:
            # Admin path — look up the requested player.
            # If player_name came from the autocomplete dropdown above, it's
            # a UCID (32 hex chars), not a display name: match it directly
            # against the parsed Foothold roster by UCID, same as the
            # self-lookup path below. This is immune to callsign prefixes,
            # unusual characters, or any other name-formatting mismatch
            # between Foothold_Ranks.lua and what's actually typed/shown.
            match = None
            if re.fullmatch(r"[0-9a-f]{32}", player_name.lower()):
                target_ucid = player_name.lower()
                match = next((n for n, d in players.items() if d.get("ucid") == target_ucid), None)
                if match is None:
                    await interaction.followup.send(
                        f"❌ No campaign stats found for that player on **`{server}`** yet.",
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
                        f"❌ Player **{_safe_code_span(player_name)}** not found in **`{server}`**.\n"
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
                        await cur.execute(
                            "SELECT ucid FROM players WHERE discord_id = %s LIMIT 1",
                            (interaction.user.id,)
                        )
                        row = await cur.fetchone()
                        own_ucid = row[0] if row else None
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
                    f"❌ No campaign stats found for you on **`{server}`** yet — "
                    f"fly a mission first, then try again.",
                    ephemeral=True)
                return

        data = players[match]

        # Session points (with callsign-stripped fallback, mirrors build_embed)
        s_pts = campaign_stats.get(match, 0)
        if s_pts == 0:
            for cs_name, cs_val in campaign_stats.items():
                if strip_callsign(cs_name) == strip_callsign(match):
                    s_pts = cs_val
                    break

        # Session stats raw (with callsign-stripped fallback)
        s_stats = session_stats_raw.get(match)
        if s_stats is None:
            for srs_name, srs_val in session_stats_raw.items():
                if strip_callsign(srs_name) == strip_callsign(match):
                    s_stats = srs_val
                    break
        s_stats = s_stats or {}

        # Daily points — reuse the same snapshot-based computation as the embed
        reset_hour = int(cfg.get("daily_reset_hour") or 0)
        schedule   = cfg.get("daily_reset_schedule") or {}
        if schedule:
            day_keys  = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
            today_key = day_keys[datetime.now(timezone.utc).weekday()]
            if today_key in schedule:
                reset_hour = int(schedule[today_key])
        name_to_ucid_fallback = {n: d.get("ucid") for n, d in players.items() if d.get("ucid")}
        # Merge per-player, not "pick one source entirely" — during a
        # transition period right after updating Foothold to 4.9.1+,
        # players already in playerStats from BEFORE the update may not
        # yet have an entry in the native ucidToName table (only
        # populated for them once they reconnect). Native takes
        # priority per-name where available; the older Ranks-based
        # cross-reference still fills in anyone the native table
        # doesn't have yet, instead of being discarded wholesale.
        name_to_ucid = {**name_to_ucid_fallback, **name_to_ucid_native}
        daily_pts_all, daily_stats_all, _ = await self._compute_daily_points(saves_dir, campaign_stats, session_stats_raw, reset_hour, node, os.path.basename(persistence_file) if persistence_file else None, name_to_ucid)
        d_pts     = daily_pts_all.get(match, 0)
        d_stats   = daily_stats_all.get(match)
        if d_stats is None:
            for ds_name, ds_val in daily_stats_all.items():
                if strip_callsign(ds_name) == strip_callsign(match):
                    d_stats = ds_val
                    break
        d_stats = d_stats or {}

        # UCID + last_seen from DCSServerBot core tables
        ucid      = data.get("ucid")
        last_seen = None
        if ucid:
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
            mission_status = f"🟢 **{server}** Mission running."
        elif srv.status == Status.PAUSED:
            mission_status = f"⏸️ **{server}** Mission paused."
        else:
            mission_status = f"⏹️ **{server}** Mission not running."

        embed = _build_player_report_embed(
            player_name=match, data=data, ucid=ucid, last_seen=last_seen,
            session_points=s_pts, daily_points=d_pts, session_stats=s_stats,
            mission_status=mission_status, daily_stats=d_stats
        )
        await interaction.followup.send(embed=embed, ephemeral=ephemeral)

    @fh_report.command(name="podium", description="Show who held a given daily-history position between two dates.")
    @app_commands.describe(
        date_from="Start date (YYYY-MM-DD)",
        date_to="End date (YYYY-MM-DD)",
        top="Show the top N positions for each day (1-50)"
    )
    async def podium(self, interaction: discord.Interaction,
                     date_from: str, date_to: str, top: app_commands.Range[int, 1, 50]):
        ephemeral = utils.get_ephemeral(interaction)
        await interaction.response.defer(ephemeral=ephemeral)

        server = self._resolve_server_from_channel(interaction)
        if not server:
            await interaction.followup.send(
                "❌ Couldn't determine which server this channel belongs to. "
                "Run this command in the channel where FH_Report posts the campaign embed.",
                ephemeral=True)
            return

        srv = self._get_server_by_instance(server)
        if srv is None:
            await interaction.followup.send(
                f"❌ Server **`{server}`** not found among configured DCSServerBot instances.",
                ephemeral=True)
            return

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

        cfg       = self._merged_cfg(server)
        saves_dir = cfg.get("saves_dir")
        if not saves_dir:
            saves_dir = os.path.join(await srv.get_missions_dir(), "Saves")
        node = srv.node

        try:
            history = self._load_daily_history(saves_dir)
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
                f"No history found for **`{server}`** between `{date_from}` and `{date_to}`.",
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
            strip_callsign_flag=_bool_cfg(cfg.get("strip_callsign"))
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
        embed.set_footer(text=f"FH_Report {FH_REPORT_RELEASE} · Read-only historical report")
        await interaction.followup.send(embed=embed, ephemeral=ephemeral)


async def setup(bot: DCSServerBot):
    await bot.add_cog(FH_Report(bot))
    logging.getLogger(__name__).info(f"  => FH_Report v{FH_REPORT_RELEASE} loaded.")
