"""
FH_Report Plugin for DCSServerBot
Reads Foothold campaign save files and posts/updates a Discord embed
with front-line status and pilot leaderboard. No database required.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone
from typing import Type

import discord
from discord.ext import tasks
from core import Plugin, TEventListener, utils
from services.bot import DCSServerBot


from .version import __version__

log = logging.getLogger(__name__)

# ── Rank thresholds from Foothold engine (zoneCommander.lua) ─────────────────
RANK_THRESHOLDS = [0, 3000, 5000, 8000, 12000, 16000, 22000, 30000, 45000, 65000, 90000]
RANK_NAMES = [
    "Recruit", "Aviator", "Airman", "Senior Airman",
    "Staff Sergeant", "Technical Sergeant", "Master Sergeant",
    "Senior Master Sergeant", "Chief Master Sergeant",
    "Second Lieutenant", "First Lieutenant"
]


# ── Helpers ───────────────────────────────────────────────────────────────────

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
        path = data.decode("utf-8").strip()
        path = os.path.normpath(path)
        try:
            await node.read_file(path)
            return path
        except FileNotFoundError:
            pass
    except FileNotFoundError:
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

        # Count slots from remainingUnits
        ru_match = re.search('\\[(?:"remainingUnits"|\'remainingUnits\')\\]=\\{(.*?)\\n  \\},', block, re.DOTALL)
        active_slots  = 0
        total_ru_slots = 0
        if ru_match:
            top_slots = re.findall(r"\n    \[(\d+)\]=\{([^}]*)\}", ru_match.group(1), re.DOTALL)
            total_ru_slots = len(top_slots)
            active_slots   = sum(1 for _, sc in top_slots if sc.strip())

        # Max slots = total slots in remainingUnits
        max_slots = total_ru_slots if total_ru_slots > 0 else min(level, 5)

        info = {"name": zone, "level": min(level, 5), "active_slots": active_slots,
                "max_slots": max_slots, "suspended": suspended}
        if side == 2:
            zones["blue"].append(info)
        elif side == 1:
            zones["red"].append(info)

    return zones


async def parse_player_stats(filepath: str, node) -> dict:
    """Parse playerStats from Foothold persistence file.
    Returns dict {player_name: campaign_points}."""
    try:
        data = await node.read_file(filepath)
        content = data.decode("utf-8")
        stats_match = re.search(
            r"zonePersistance\[[\"']playerStats[\"']\] = \{(.*?)^\}",
            content, re.DOTALL | re.MULTILINE
        )
        if not stats_match:
            return {}
        block = stats_match.group(1)
        results = {}
        for m in re.finditer(r"\[[\"']([^\"']+)[\"']\]=\{([^}]+)\}", block, re.DOTALL):
            pts_m = re.search('\\[(?:"Points"|\'Points\')\\]=(\\d+)', m.group(2))
            if pts_m:
                results[m.group(1)] = int(pts_m.group(1))
        return results
    except Exception:
        return {}


async def parse_ranks(filepath: str, excluded_ucids: list[str], node) -> dict:
    """Parse Foothold_Ranks.lua. Returns pilot dict sorted by credits desc.
    Pilots whose UCID is in excluded_ucids are omitted."""
    data = await node.read_file(filepath)
    content = data.decode("utf-8")
    # Build set of excluded player names from ucidToName table
    excluded_names: set[str] = set()
    for ucid in excluded_ucids:
        m = re.search(rf"\['{re.escape(ucid)}'\]=\"([^\"]+)\"", content)
        if m:
            excluded_names.add(m.group(1))

    # Build name->ucid mapping from ucidToName table
    name_to_ucid = {}
    ucid_pattern = r"\[[\'\"]([a-f0-9]{32})[\'\"]\]=[\'\"]([^\'\"]+)[\'\"]"
    for ucid_m in re.finditer(ucid_pattern, content):
        name_to_ucid[ucid_m.group(2)] = ucid_m.group(1)

    players = {}
    block_pattern = r"\[[\"']([^\"']+)[\"']\]=\{([^}]+)\}"
    for m in re.finditer(block_pattern, content):
        name  = m.group(1)
        block = m.group(2)
        credit_m = re.search('\\[(?:"credits"|\'credits\')\\]=([\\d.]+)', block)
        if not credit_m:
            continue
        if name in excluded_names:
            continue
        # Skip invalid or empty names
        clean_name = name.strip()
        if not clean_name or len(clean_name) < 2:
            continue
        players[clean_name] = {
            "credits": float(credit_m.group(1)),
            "ucid":    name_to_ucid.get(clean_name),
        }

    return dict(sorted(players.items(), key=lambda x: x[1]["credits"], reverse=True))


def strip_callsign(name: str) -> str:
    """Remove flight callsign prefix from pilot name.
    Handles separators (|, /, backslash, ,) and callsign patterns (WORD N-N).
    Preserves squadron tags like [MA] at the start."""
    # Step 1 — split on separator, keep rightmost part
    for sep in ['|', '/', chr(92), ',']:
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


# ── Punishment thresholds ─────────────────────────────────────────────────────
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


def _lb_title(points_order: str) -> str:
    """Build leaderboard field title based on points_order."""
    if points_order == "BR":
        return "\n🏆 __Pilot Leaderboard · by Rank (R: Rank · S: Session)__"
    if points_order == "BS":
        return "\n📊 __Session Leaderboard · by Current Session (S: Session · R: Rank)__"
    if points_order in ("S", "2S"):
        return "\n📊 __Session Leaderboard · by Current Session__"
    return "\n🏆 __Pilot Leaderboard · by Rank__"



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


def build_embed(zones: dict, players: dict, campaign_name: str,
                max_zones: int | None, max_pilots: int | None,
                bar_length: int, slot_status: int = 0,
                zone_name_length: int = 16,
                max_pilots_2t: int | None = None,
                punishment_points: dict | None = None,
                show_punishment: int = 0,
                show_all_pilots: int = 0,
                strip_callsign_flag: int = 0,
                campaign_stats: dict | None = None,
                points_order: str = "T") -> discord.Embed:
    """Build the Discord embed from parsed Foothold data."""
    timestamp  = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    blue_count  = len(zones["blue"])
    red_count   = len(zones["red"])
    neutral_count = zones.get("neutral", 0)
    total         = blue_count + red_count + neutral_count
    active_total  = blue_count + red_count

    # Progress bar — ANSI colored block characters inside ```ansi block.
    # Uses single-width chars (█) instead of double-width emoji so the line
    # never wraps regardless of bar_length. Blue=\u001b[34m Red=\u001b[91m
    # Neutral=\u001b[37m Reset=\u001b[0m. Works on Discord Desktop & Browser.
    pct_blue     = round(blue_count / active_total * 100) if active_total > 0 else 50
    pct_red      = 100 - pct_blue
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

    # BLUE zones — actives first sorted by level+slots, suspended last
    blue_active    = [z for z in zones["blue"] if not z.get("suspended")]
    blue_suspended = [z for z in zones["blue"] if z.get("suspended")]
    blue_active    = sorted(blue_active, key=lambda z: (z["level"], z.get("active_slots", 0)), reverse=True)
    blue_suspended = sorted(blue_suspended, key=lambda z: z["level"], reverse=True)
    blue_sorted    = blue_active + blue_suspended
    limit          = max_zones if max_zones else len(blue_sorted)
    blue_lines     = []
    for z in blue_sorted[:limit]:
        lvl = min(z["level"], 5)
        if slot_status == 1 and not z.get("suspended"):
            active    = z.get("active_slots", lvl)
            max_s     = z.get("max_slots", lvl)
            stars     = "🔹" * active + "◇" * (max_s - active)
        else:
            stars  = "🔹" * lvl
        blue_lines.append(f"`{z['name'][:zone_name_length]}` {stars}")
    if max_zones and len(blue_sorted) > max_zones:
        blue_lines.append(f"*+ {len(blue_sorted) - max_zones} more bases*")
    blue_lines.append(".")
    blue_text = "\n".join(blue_lines) if blue_lines else "—"

    # RED zones — actives first sorted by level+slots, suspended last
    red_active    = [z for z in zones["red"] if not z.get("suspended")]
    red_suspended = [z for z in zones["red"] if z.get("suspended")]
    red_active    = sorted(red_active, key=lambda z: (z["level"], z.get("active_slots", 0)), reverse=True)
    red_suspended = sorted(red_suspended, key=lambda z: z["level"], reverse=True)
    red_sorted    = red_active + red_suspended
    limit         = max_zones if max_zones else len(red_sorted)
    red_lines     = []
    for z in red_sorted[:limit]:
        lvl = min(z["level"], 5)
        if slot_status == 1 and not z.get("suspended"):
            active    = z.get("active_slots", lvl)
            max_s     = z.get("max_slots", lvl)
            stars     = "🔺" * active + "△" * (max_s - active)
        else:
            stars  = "🔺" * lvl
        red_lines.append(f"`{z['name'][:zone_name_length]}` {stars}")
    if max_zones and len(red_sorted) > max_zones:
        red_lines.append(f"*+ {len(red_sorted) - max_zones} more bases*")
    red_text = "\n".join(red_lines) if red_lines else "—"

    # Pilot leaderboard — apply session stats and ordering
    cs = campaign_stats or {}

    # Add session_points to each player
    # Skip if hook already set session_points (hook value takes priority)
    for name, data in players.items():
        if "session_points" in data:
            continue  # hook already calculated this value
        s_pts = cs.get(name, 0)
        if s_pts == 0:
            for cs_name, cs_pts in cs.items():
                if strip_callsign(cs_name) == strip_callsign(name):
                    s_pts = cs_pts
                    break
        data["session_points"] = s_pts

    # Determine sort key and display flags from points_order
    order_by_session = points_order in ("S", "BS", "2S")
    show_rank        = points_order in ("R", "BR", "BS", "2R", "2S")
    show_credits_session     = points_order in ("S", "BR", "BS", "2R", "2S")

    if order_by_session:
        pilot_items = sorted(players.items(), key=lambda x: x[1].get("session_points", 0), reverse=True)
    else:
        pilot_items = list(players.items())  # already sorted by total credits

    # In dual-table modes use max_pilots_2t for first table if defined
    _limit_first = (max_pilots_2t if max_pilots_2t else max_pilots) if points_order in ("2R", "2S") else max_pilots
    total_pilots_count = len(pilot_items)
    if _limit_first:
        pilot_items = pilot_items[:_limit_first]
    hidden_pilots = total_pilots_count - len(pilot_items)

    medals = ["🥇", "🥈", "🥉"] + ["🎖️"] * 50
    pilot_lines = []
    pp = punishment_points or {}
    for i, (name, data) in enumerate(pilot_items):
        credits = int(data["credits"])
        rank    = get_rank(credits)
        medal   = data.get("custom_medal") or (medals[i] if i < len(medals) else "•")
        display = strip_callsign(name) if strip_callsign_flag else name
        short   = display.replace('`', '') if len(display) <= 22 else display[:20].replace('`', '') + '..'
        # Hook overrides
        rank    = data.get("custom_rank") or rank
        hide_credits = data.get("hide_credits", False)
        s_pts   = data.get("session_points", 0)

        # Build points string based on points_order
        hide_session = data.get("hide_session", False)

        def _pts(r, s):
            """Build points string respecting hide flags."""
            show_r = not hide_credits and r is not None
            show_s = not hide_session and s is not None
            if show_r and show_s:
                return f"(R: {r:,} · S: {s:,})" if s else f"(R: {r:,})"
            elif show_r:
                return f"(R: {r:,})"
            elif show_s:
                return f"(S: {s:,})"
            return ""

        if points_order == "R":
            pts_str = "" if hide_credits else f"(R: {credits:,})"
        elif points_order == "S":
            pts_str = "" if hide_session else f"(S: {s_pts:,})"
        elif points_order == "BR":
            pts_str = _pts(credits, s_pts if s_pts else None)
        elif points_order == "BS":
            show_r = not hide_credits
            show_s = not hide_session and s_pts
            if show_s and show_r:
                pts_str = f"(S: {s_pts:,} · R: {credits:,})"
            elif show_s:
                pts_str = f"(S: {s_pts:,})"
            elif show_r:
                pts_str = f"(R: {credits:,})"
            else:
                pts_str = ""
        elif points_order == "2R":
            pts_str = "" if hide_credits else f"(R: {credits:,})"
        else:  # 2S — primary table is session
            pts_str = "" if hide_session else (f"(S: {s_pts:,})" if s_pts else "(S: 0)")

        pilot_lines.append(f"{medal} `{short}` — **{rank}** {pts_str}".rstrip())
        # Punishment badge — on rank table always; on session table only when S is the only table
        show_punishment_here = show_punishment and points_order != "2S"
        if show_punishment_here:
            ucid = data.get("ucid")
            if "hook_punishment" in data:
                pts = data["hook_punishment"]
            elif pp and ucid:
                pts = pp.get(ucid, 0)
            else:
                pts = 0
            badge = get_punishment_badge(pts, short, data.get("punishment_icon", ""), data.get("punishment_label", ""), data.get("punishment_pre_icon", ""))
            if badge:
                pilot_lines.append(badge)
    pilots_text = "\n".join(pilot_lines) if pilot_lines else "—"

    embed = discord.Embed(
        title=f"📡  {campaign_name}",
        description=(
            f"**Front Status — {timestamp}**\n\n"
            f"{progress}"
        ),
        color=0x3498DB
    )
    embed.add_field(
        name=f"🔵 BLUE Zones ({blue_count})",
        value=blue_text[:1024],
        inline=True
    )
    embed.add_field(
        name=f"🔴 RED Zones ({red_count})",
        value=red_text[:1024],
        inline=True
    )

    if show_all_pilots == 1:
        # ── Option B: split into multiple fields, show all pilots ─────────────
        # Add more pilots note if max_pilots was applied
        if hidden_pilots > 0:
            pilot_lines.append(f"*+ {hidden_pilots} more pilots*")

        FIELD_LIMIT = 1020
        chunks = []
        current_chunk, current_len = [], 0
        for line in pilot_lines:
            line_len = len(line) + 1
            if current_len + line_len > FIELD_LIMIT and current_chunk:
                chunks.append("\n".join(current_chunk))
                current_chunk, current_len = [line], line_len
            else:
                current_chunk.append(line)
                current_len += line_len
        if current_chunk:
            chunks.append("\n".join(current_chunk))
        for i, chunk in enumerate(chunks):
            embed.add_field(
                name=(_lb_title(points_order)) if i == 0 else ("📊 __Session Leaderboard (cont.)__" if points_order in ('S','BS','2S') else "🎖️ __Leaderboard (cont.)__"),
                value=("\n" + chunk) if i == 0 else chunk,
                inline=False
            )
    else:
        # ── Option A (default): single field, cut at limit, show + X more ─────
        FIELD_LIMIT = 1020
        visible_lines, used = [], 0
        for i, line in enumerate(pilot_lines):
            line_len = len(line) + 1
            # Reserve space for more pilots label
            lines_after = len(pilot_lines) - i - 1
            total_hidden = hidden_pilots + lines_after
            more_label = f"\n*+ {total_hidden} more pilots*" if total_hidden > 0 else ""
            if used + line_len + len(more_label) > FIELD_LIMIT:
                break
            visible_lines.append(line)
            used += line_len
        lines_not_shown = len(pilot_lines) - len(visible_lines)
        total_hidden_final = hidden_pilots + lines_not_shown
        pilots_value = "\n" + "\n".join(visible_lines)
        if total_hidden_final > 0:
            pilots_value += f"\n*+ {total_hidden_final} more pilots*"
        embed.add_field(
            name=_lb_title(points_order),
            value=pilots_value[:1024],
            inline=False
        )

    # ── 2R/2S: add second leaderboard ────────────────────────────────────────
    if points_order in ("2R", "2S"):
        # For 2R: second table = session. For 2S: second table = rank (already sorted)
        if points_order == "2R":
            second_items = sorted(players.items(), key=lambda x: x[1].get("session_points", 0), reverse=True)
            second_title = "📊 __Session Leaderboard · by Current Session__"
            second_cont  = "📊 __Session Leaderboard (cont.)__"
        else:  # 2S: second table = rank order
            second_items = sorted(players.items(), key=lambda x: x[1]["credits"], reverse=True)
            second_title = "🏆 __Pilot Leaderboard · by Rank__"
            second_cont  = "🎖️ __Leaderboard (cont.)__"

        if second_items:
            # Use max_pilots_2t for second table if defined, else max_pilots
            _limit_2t     = max_pilots_2t if max_pilots_2t else max_pilots
            total_second  = len(second_items)
            if _limit_2t:
                second_items = second_items[:_limit_2t]
            hidden_second = total_second - len(second_items)
            second_lines = []
            s_medals = ["🥇", "🥈", "🥉"] + ["🎖️"] * 50
            for i, (name, data) in enumerate(second_items):
                s_credits = int(data["credits"])
                s_rank    = data.get("custom_rank") or get_rank(s_credits)
                s_display = strip_callsign(name) if strip_callsign_flag else name
                s_short   = s_display.replace('`', '') if len(s_display) <= 22 else s_display[:20].replace('`', '') + '..'
                s_medal   = data.get("custom_medal") or (s_medals[i] if i < len(s_medals) else "•")
                s_pts        = data.get("session_points", 0)
                s_hide       = data.get("hide_credits", False)
                s_hide_session = data.get("hide_session", False)
                if points_order == "2R":
                    line = f"{s_medal} `{s_short}` — **{s_rank}**" if s_hide_session else f"{s_medal} `{s_short}` — **{s_rank}** (S: {s_pts:,})"
                else:  # 2S second table = rank
                    line = f"{s_medal} `{s_short}` — **{s_rank}**" if s_hide else f"{s_medal} `{s_short}` — **{s_rank}** (R: {s_credits:,})"
                second_lines.append(line)
                # Punishment badge on second table only for 2S (rank table)
                if show_punishment and points_order == "2S":
                    s_ucid = data.get("ucid")
                    if "hook_punishment" in data:
                        s_pts_p = data["hook_punishment"]
                    elif pp and s_ucid:
                        s_pts_p = pp.get(s_ucid, 0)
                    else:
                        s_pts_p = 0
                    s_badge = get_punishment_badge(s_pts_p, s_short, data.get("punishment_icon", ""), data.get("punishment_label", ""), data.get("punishment_pre_icon", ""))
                    if s_badge:
                        second_lines.append(s_badge)
            if hidden_second > 0:
                second_lines.append(f"*+ {hidden_second} more pilots*")

            # Split into chunks
            FIELD_LIMIT = 1020
            s_chunks, s_current, s_len = [], [], 0
            for line in second_lines:
                ll = len(line) + 1
                if s_len + ll > FIELD_LIMIT and s_current:
                    s_chunks.append("\n".join(s_current))
                    s_current, s_len = [line], ll
                else:
                    s_current.append(line)
                    s_len += ll
            if s_current:
                s_chunks.append("\n".join(s_current))

            for i, chunk in enumerate(s_chunks):
                embed.add_field(
                    name=("\n" + second_title) if i == 0 else second_cont,
                    value=("\n" + chunk) if i == 0 else chunk,
                    inline=False
                )

    embed.set_footer(text=f"{campaign_name} • Updated automatically")
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
        self._cycle_index: dict = {}
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
        # Set interval from DEFAULT if configured, then start the loop
        raw      = self.locals or {}
        interval = (raw.get("DEFAULT") or {}).get("update_interval", 300)
        self.updater.change_interval(seconds=int(interval))
        utils.safe_start(self.updater)

    async def cog_unload(self) -> None:
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

        # Iterate all DCSSB servers — same pattern as Pretense.
        # Config is looked up by instance name (the key used in fh_report.yaml)
        # rather than server.name (the long DCS display name), so existing yaml
        # configs require no changes.
        for server in self.bot.servers.values():
            try:
                instance_name = server.instance.name
                srv_cfg = raw.get(instance_name)
                if not srv_cfg:
                    continue
                # Merge DEFAULT + instance overrides fresh each cycle (like Pretense)
                cfg = dict(default_cfg)
                cfg.update(srv_cfg)
                await self._update_server(server, cfg)
            except Exception as e:
                self.log.error(
                    f"FH_Report [{server.instance.name}]: unexpected error: {e}", exc_info=True
                )

    @updater.before_loop
    async def before_updater(self):
        await self.bot.wait_until_ready()

    def _resolve_points_order(self, server_name: str, cfg: dict) -> str:
        """Parse points_order — supports comma-separated cycle list."""
        raw   = str(cfg.get("points_order") or "R").strip()
        items = [x.strip() for x in raw.split(",") if x.strip()]
        if len(items) <= 1:
            return items[0] if items else "R"
        idx   = self._cycle_index.get(server_name, 0)
        value = items[idx % len(items)]
        self._cycle_index[server_name] = (idx + 1) % len(items)
        return value

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

        node = server.node

        persistence_file = await find_persistence_file(saves_dir, node)
        if not persistence_file:
            self.log.warning(f"FH_Report [{instance_name}]: no foothold_*.lua found in {saves_dir}")
            return

        ranks_file = os.path.join(saves_dir, "Foothold_Ranks.lua")
        try:
            await node.read_file(ranks_file)
        except FileNotFoundError:
            self.log.warning(f"FH_Report [{instance_name}]: Foothold_Ranks.lua not found in {saves_dir}")
            return

        try:
            excluded_ucids = cfg.get("excluded_ucids") or []
            zones          = await parse_zones(persistence_file, node)
            players        = await parse_ranks(ranks_file, excluded_ucids, node)
            campaign_stats = await parse_player_stats(persistence_file, node)
        except Exception as e:
            self.log.error(f"FH_Report [{instance_name}]: error parsing data: {e}")
            return

        # Optional private hook — post-processes players dict
        if _HAS_HOOK:
            try:
                players = _fh_hook.post_process(players, cfg, instance_name, campaign_stats)
            except Exception:
                pass

        show_punishment   = int(cfg.get("show_punishment") or 0)
        punishment_points = {}
        if show_punishment:
            punishment_points = await self._fetch_punishment_points()

        embed = build_embed(
            zones               = zones,
            players             = players,
            campaign_name       = cfg.get("campaign_name", "Foothold Campaign"),
            max_zones           = cfg.get("max_zones") or None,
            max_pilots          = cfg.get("max_pilots") or None,
            bar_length          = int(cfg.get("bar_length") or 40),
            slot_status         = int(cfg.get("slot_status") or 0),
            punishment_points   = punishment_points,
            show_punishment     = show_punishment,
            show_all_pilots     = int(cfg.get("show_all_pilots") or 0),
            strip_callsign_flag = int(cfg.get("strip_callsign") or 0),
            zone_name_length    = max(8, min(24, int(cfg.get("zone_name_length") or 16))),
            max_pilots_2t       = cfg.get("max_pilots_2t") or None,
            campaign_stats      = campaign_stats,
            points_order        = self._resolve_points_order(instance_name, cfg),
        )

        try:
            msg_id = self._message_ids.get(instance_name)
            if msg_id:
                try:
                    msg = await channel.fetch_message(msg_id)
                    await msg.edit(embed=embed)
                    return
                except discord.NotFound:
                    self.log.warning(f"FH_Report [{instance_name}]: previous message not found, posting new one.")
                    self._message_ids.pop(instance_name, None)

            msg = await channel.send(embed=embed)
            self._message_ids[instance_name] = msg.id
            self._save_message_ids()

        except discord.HTTPException as e:
            self.log.error(f"FH_Report [{instance_name}]: Discord error: {e}")
async def setup(bot: DCSServerBot):
    await bot.add_cog(FH_Report(bot))
