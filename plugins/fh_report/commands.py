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
from datetime import datetime, timezone, timedelta
from typing import Type

import discord
from discord.ext import tasks
from core import Plugin, TEventListener, utils
from services.bot import DCSServerBot


from .version import __version__

log = logging.getLogger(__name__)

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
        # Extract only the filename from the path stored in foothold.status.
        # The full path is irrelevant — it may be a Windows absolute path that
        # is invalid on Linux/Wine hosts. The file always lives in saves_dir.
        raw_path = data.decode("utf-8").strip()
        filename = os.path.basename(raw_path)
        path     = os.path.join(saves_dir, filename)
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
                # Count slots 1..level — slot active if it has unit name strings
                # Only check the first min(level,5) slots — these are the
                # primary upgrade slots. Higher-numbered slots serve other
                # purposes and are excluded to avoid misleading displays.
                display_slots = min(level, 5)
                for idx in range(1, display_slots + 1):
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
            r"zonePersistance\[[\"']playerStats[\"']\]\s*=\s*\{",
            content
        )
        if not stats_match:
            return {}
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
        # Find each player entry by name key
        for m in re.finditer(r"\[[\"']([^\"']+)[\"']\]\s*=\s*\{", block):
            name      = m.group(1)
            blk_start = m.end()
            # Count braces to find end of this player's block
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
            if pts_m:
                results[name] = int(pts_m.group(1))
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

    # Find the players block first using brace counting
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

    # Extract each player using brace counting — handles nested sub-tables
    # introduced in Foothold v4.5 (career, aircraft fields)
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

        # Extract career stats (Foothold v4.5+)
        # CAREER_STAT IDs: FlightSeconds=1, HelicopterSeconds=3,
        # TotalKills=10, ConventionalCarrierTraps=8,
        # FuelReceivedLbs=30, PilotDeaths=21
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

        # Extract aircraft stats — sum flight seconds across all aircraft types
        aircraft_helo_seconds = 0.0
        aircraft_m = re.search(r'\[(?:"aircraft"|\'aircraft\')\]\s*=\s*\{', block)
        if aircraft_m:
            ab = block.find('{', aircraft_m.end() - 1)
            ad, aj = 1, ab + 1
            while aj < len(block) and ad > 0:
                if block[aj] == '{': ad += 1
                elif block[aj] == '}': ad -= 1
                aj += 1
            # We don't parse individual aircraft here — helo time comes from career[3]

        players[clean_name] = {
            "credits": float(credit_m.group(1)),
            "ucid":    name_to_ucid.get(clean_name),
            "career":  career,
        }

    return dict(sorted(players.items(), key=lambda x: x[1]["credits"], reverse=True))



async def deduplicate_ranks(ranks_file: str, persistence_file, node) -> bool:
    """Detect and fix duplicate player entries in Foothold_Ranks.lua caused by
    callsign changes. The entry with a UCID in ucidToName is canonical; its
    name is cleaned via strip_callsign(). Credits and lastSeen are merged.
    Returns True if any fix was applied and the file was rewritten."""

    ranks_data = (await node.read_file(ranks_file)).decode("utf-8")

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
        name_with_ucid = next((n for n in raw_names if n in raw_to_ucid), None)
        if not name_with_ucid:
            continue

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

    # Write using atomic tmp-file pattern (same as fh_control's _write_lua)
    tmp = ranks_file + ".fhrep.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(ranks_data)
        os.replace(tmp, ranks_file)
    except Exception as e:
        import logging as _lg2
        _lg2.getLogger(__name__).error(f"FH_Report: deduplicate_ranks write failed: {e}")
        return False
    return True


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
    titles = {
        "R":   "\n🏆 __Pilot Leaderboard · by Rank__",
        "S":   "\n📊 __Session Leaderboard · by Current Session__",
        "D":   "\n📅 __Daily Leaderboard · by Today\'s Points__",
        "BR":  "\n🏆 __Pilot Leaderboard · by Rank (R · S · D)__",
        "BS":  "\n📊 __Session Leaderboard · by Current Session (S · R · D)__",
        "BD":  "\n📅 __Daily Leaderboard · by Today\'s Points (D · R · S)__",
        "BDS": "\n📅 __Daily Leaderboard · by Today\'s Points (D · S · R)__",
        "2R":  "\n🏆 __Pilot Leaderboard · by Rank__",
        "2S":  "\n📊 __Session Leaderboard · by Current Session__",
        "2D":  "\n📅 __Daily Leaderboard · by Today\'s Points__",
        "2DS": "\n📅 __Daily Leaderboard · by Today\'s Points__",
        "3R":  "\n🏆 __Pilot Leaderboard · by Rank__",
        "3S":  "\n📊 __Session Leaderboard · by Current Session__",
        "3D":  "\n📅 __Daily Leaderboard · by Today\'s Points__",
        "3DS": "\n📅 __Daily Leaderboard · by Today\'s Points__",
    }
    return titles.get(points_order, "\n🏆 __Pilot Leaderboard · by Rank__")



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


def _build_pilot_card(career: dict) -> str | None:
    """Build a one-line pilot career card from career stats dict.
    CAREER_STAT IDs (Foothold v4.5):
      1=FlightSeconds  3=HelicopterSeconds  8=ConventionalCarrierTraps
      10=TotalKills    21=PilotDeaths       30=FuelReceivedLbs
    Returns None if all values are zero."""
    flight_h  = int(career.get(1, 0)) // 3600
    helo_h    = int(career.get(3, 0)) // 3600
    kills     = int(career.get(10, 0))
    traps     = int(career.get(8, 0))
    refuels   = int(career.get(30, 0)) // 10000  # rough conversion lbs→events
    deaths    = int(career.get(21, 0))

    parts = []
    if flight_h > 0:
        parts.append(f"✈️ {flight_h} hrs")
    # Show helo time only if > 0 and different from total flight time
    if helo_h > 0 and helo_h != flight_h:
        parts.append(f"🚁 {helo_h} hrs")
    if kills > 0:
        parts.append(f"🎯 {kills} kills")
    if traps > 0:
        parts.append(f"🚢 {traps} traps")
    if refuels > 0:
        parts.append(f"⛽ {refuels} refuels")
    if deaths > 0:
        parts.append(f"💀 {deaths} deaths")

    if not parts:
        return None
    return "·  " + "  ".join(parts)


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
                points_order: str = "T",
                bar_style_emoji: bool = False,
                daily_points: dict | None = None,
                show_pilot_card: bool = False) -> discord.Embed:
    """Build the Discord embed from parsed Foothold data."""
    timestamp  = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
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
        if slot_status and not z.get("suspended"):
            display_lvl    = min(z["level"], 5)
            display_active = min(z.get("active_slots", display_lvl), display_lvl)
            stars = "🔹" * display_active + "◇" * (display_lvl - display_active)
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
        if slot_status and not z.get("suspended"):
            display_lvl    = min(z["level"], 5)
            display_active = min(z.get("active_slots", display_lvl), display_lvl)
            stars = "🔺" * display_active + "△" * (display_lvl - display_active)
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
    dp          = daily_points or {}  # {name: daily_pts}
    has_daily   = bool(dp)

    order_by_session = points_order in ("S", "BS", "2S", "3S")
    order_by_daily   = points_order in ("D", "BD", "BDS", "2D", "2DS", "3D", "3DS")

    if order_by_session:
        pilot_items = [(n, d) for n, d in sorted(players.items(), key=lambda x: x[1].get("session_points", 0), reverse=True)
                       if d.get("session_points", 0) > 0]
    elif order_by_daily:
        pilot_items = [(n, d) for n, d in sorted(players.items(), key=lambda x: dp.get(x[0], 0), reverse=True)
                       if dp.get(n, 0) > 0]
    else:
        pilot_items = [(n, d) for n, d in players.items() if d.get("credits", 0) > 0]

    # In dual-table modes use max_pilots_2t for first table if defined
    _limit_3t    = max_pilots_3t or max_pilots_2t or max_pilots
    _limit_2t_val = max_pilots_2t or max_pilots
    _limit_first = _limit_3t if points_order in ("3R", "3S", "3D", "3DS") else (_limit_2t_val if points_order in ("2R", "2S", "2D", "2DS") else max_pilots)
    total_pilots_count = len(pilot_items)
    # Apply first table limit and track surplus for cascade
    _surplus = 0
    if _limit_first:
        _actual_first = len(pilot_items)
        pilot_items   = pilot_items[:_limit_first]
        _surplus      = max(0, _limit_first - len(pilot_items))
    else:
        _surplus = 0
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
        d_pts        = dp.get(name, 0)
        show_d       = has_daily and d_pts > 0

        def _tri(first, second, third):
            """Build R·S·D string with only available/non-hidden values."""
            parts = [p for p in [first, second, third] if p]
            return f"({'  ·  '.join(parts)})" if parts else ""

        def _r():  return f"R: {credits:,}" if not hide_credits else None
        def _s():  return f"S: {s_pts:,}"   if not hide_session and s_pts else None
        def _d():  return f"D: {d_pts:,}"   if show_d else None

        if points_order == "R":
            pts_str = "" if hide_credits else f"(R: {credits:,})"
        elif points_order == "S":
            pts_str = "" if hide_session else f"(S: {s_pts:,})"
        elif points_order == "D":
            pts_str = f"(D: {d_pts:,})" if show_d else ""
        elif points_order == "BR":
            pts_str = _tri(_r(), _s(), _d())
        elif points_order == "BS":
            pts_str = _tri(_s(), _r(), _d())
        elif points_order == "BD":
            pts_str = _tri(_d(), _r(), _s())
        elif points_order == "BDS":
            pts_str = _tri(_d(), _s(), _r())
        elif points_order == "2R":
            pts_str = _tri(_r(), _s(), _d())
        elif points_order == "2D":
            pts_str = _tri(_d(), _r(), _s())
        elif points_order == "2DS":
            pts_str = _tri(_d(), _s(), _r())
        elif points_order == "3R":
            pts_str = _tri(_r(), _s(), _d())
        elif points_order == "3S":
            pts_str = _tri(_s(), _r(), _d())
        elif points_order == "3D":
            pts_str = _tri(_d(), _r(), _s())
        elif points_order == "3DS":
            pts_str = _tri(_d(), _s(), _r())
        else:  # 2S — primary table is session
            pts_str = _tri(_s(), _r(), _d()) if s_pts else "(S: 0)"

        pilot_lines.append(f"{medal} `{short}` — **{rank}** {pts_str}".rstrip())
        # Pilot career card (show_pilot_card) — always shown when rank table is present
        if show_pilot_card:
            card = _build_pilot_card(data.get("career") or {})
            if card:
                pilot_lines.append(card)
        # Punishment badge — on rank table always; on session table only when S is the only table
        # Badge goes on the table with highest priority R>S>D
        # For single/B modes: always on this (only) table
        # For 2x: on first table only if first table key is R (2R)
        # For 3x: on first table only if first table key is R (3R)
        _first_is_rank = points_order in ("R", "BR", "BS", "BD", "BDS", "2R", "3R")
        _is_multi      = points_order.startswith("2") or points_order.startswith("3")
        show_punishment_here = show_punishment and (_first_is_rank or not _is_multi)
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

    # For compound modes, skip first table entirely if no pilots
    _is_compound = points_order in ("2R", "2S", "2D", "2DS", "3R", "3S", "3D", "3DS")
    if _is_compound and not pilot_lines:
        pass  # skip first table — no data
    elif show_all_pilots:
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
    elif not (_is_compound and not pilot_lines):
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

    # ── 2x modes: add second leaderboard ─────────────────────────────────────
    if points_order in ("2R", "2S", "2D", "2DS"):
        if points_order == "2R":
            second_items = [(n, d) for n, d in sorted(players.items(), key=lambda x: x[1].get("session_points", 0), reverse=True)
                            if d.get("session_points", 0) > 0]
            second_title = "📊 __Session Leaderboard · by Current Session__"
            second_cont  = "📊 __Session Leaderboard (cont.)__"
        elif points_order == "2S":
            second_items = [(n, d) for n, d in sorted(players.items(), key=lambda x: x[1]["credits"], reverse=True)
                            if d.get("credits", 0) > 0]
            second_title = "🏆 __Pilot Leaderboard · by Rank__"
            second_cont  = "🎖️ __Leaderboard (cont.)__"
        elif points_order == "2D":
            second_items = [(n, d) for n, d in sorted(players.items(), key=lambda x: x[1]["credits"], reverse=True)
                            if d.get("credits", 0) > 0]
            second_title = "🏆 __Pilot Leaderboard · by Rank__"
            second_cont  = "🎖️ __Leaderboard (cont.)__"
        else:  # 2DS: second table = session
            second_items = [(n, d) for n, d in sorted(players.items(), key=lambda x: x[1].get("session_points", 0), reverse=True)
                            if d.get("session_points", 0) > 0]
            second_title = "📊 __Session Leaderboard · by Current Session__"
            second_cont  = "📊 __Session Leaderboard (cont.)__"

        if second_items:
            # Use max_pilots_2t + cascade surplus from first table
            _limit_2t     = (max_pilots_2t if max_pilots_2t else max_pilots) or 0
            _limit_2t_eff = _limit_2t + _surplus if _limit_2t else None
            total_second  = len(second_items)
            if _limit_2t_eff:
                second_items = second_items[:_limit_2t_eff]
            # Update surplus for potential 3rd table
            _surplus      = max(0, _limit_2t_eff - len(second_items)) if _limit_2t_eff else 0
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
                s_d_pts   = dp.get(name, 0)
                s_show_d  = has_daily and s_d_pts > 0
                def _sr(): return f"R: {s_credits:,}" if not s_hide else None
                def _ss(): return f"S: {s_pts:,}" if not s_hide_session and s_pts else None
                def _sd(): return f"D: {s_d_pts:,}" if s_show_d else None
                def _s_tri(a, b, c):
                    parts = [p for p in [a, b, c] if p]
                    return f"({'  ·  '.join(parts)})" if parts else ""
                if points_order == "2R":
                    pts_part = _s_tri(_ss(), _sr(), _sd())
                elif points_order == "2S":
                    pts_part = _s_tri(_sr(), _ss(), _sd())
                elif points_order == "2D":
                    pts_part = _s_tri(_sr(), _ss(), _sd())
                else:  # 2DS second = session
                    pts_part = _s_tri(_ss(), _sr(), _sd())
                line = f"{s_medal} `{s_short}` — **{s_rank}** {pts_part}".rstrip()
                second_lines.append(line)
                # Punishment badge on second table only for 2S (rank table)
                # Badge on second table when second table has higher priority than first
                # 2S: 2nd=R (R>S) ✓  |  2D: 2nd=R (R>D) ✓  |  2DS: 2nd=S (S>D) ✓  |  2R: 2nd=S (R already on 1st) ✗
                _2nd_key = {"2S": "R", "2D": "R", "2DS": "S", "2R": "S"}.get(points_order, "")
                _1st_priority = {"R": 0, "S": 1, "D": 2}
                _badge_key = {"R": 0, "S": 1, "D": 2}
                _show_on_2nd = (
                    show_punishment and
                    _badge_key.get(_2nd_key, 9) < _1st_priority.get(
                        "R" if points_order == "2R" else
                        "S" if points_order == "2S" else
                        "D", 9
                    )
                )
                if _show_on_2nd:
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

            # Skip if no data
            if not second_lines:
                pass
            else:
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

    # ── 3x modes: add second and third leaderboard ───────────────────────────
    if points_order in ("3R", "3S", "3D", "3DS"):
        # Define table order: [2nd_key, 3rd_key]
        # 3R: rank / session / daily  → 2nd=session, 3rd=daily
        # 3S: session / rank / daily  → 2nd=rank,    3rd=daily
        # 3D: daily / rank / session  → 2nd=rank,    3rd=session
        # 3DS: daily / session / rank → 2nd=session, 3rd=rank
        def _sorted_by(key: str):
            if key == "R":
                return sorted(players.items(), key=lambda x: x[1]["credits"], reverse=True)
            elif key == "S":
                return sorted(players.items(), key=lambda x: x[1].get("session_points", 0), reverse=True)
            else:  # D
                return sorted(players.items(), key=lambda x: dp.get(x[0], 0), reverse=True)

        def _title(key: str):
            if key == "R": return ("🏆 __Pilot Leaderboard · by Rank__", "🎖️ __Leaderboard (cont.)__")
            if key == "S": return ("📊 __Session Leaderboard · by Current Session__", "📊 __Session Leaderboard (cont.)__")
            return ("📅 __Daily Leaderboard · by Today's Points__", "📅 __Daily Leaderboard (cont.)__")

        order_map = {
            "3R":  ("S", "D"),
            "3S":  ("R", "D"),
            "3D":  ("R", "S"),
            "3DS": ("S", "R"),
        }
        second_key, third_key = order_map[points_order]
        _lim_3 = _limit_3t if _limit_3t else max_pilots

        for tbl_key in (second_key, third_key):
            tbl_items = _sorted_by(tbl_key)
            # Skip daily table if no daily data
            if tbl_key == "D" and not has_daily:
                continue
            # Filter out players with zero points for this table's key
            if tbl_key == "R":
                tbl_items = [(n, d) for n, d in tbl_items if d.get("credits", 0) > 0]
            elif tbl_key == "S":
                tbl_items = [(n, d) for n, d in tbl_items if d.get("session_points", 0) > 0]
            else:  # D
                tbl_items = [(n, d) for n, d in tbl_items if dp.get(n, 0) > 0]
            if not tbl_items:
                continue
            tbl_title, tbl_cont = _title(tbl_key)
            total_tbl = len(tbl_items)
            if _lim_3:
                _lim_3_eff = _lim_3 + _surplus if _lim_3 else None
            if _lim_3_eff:
                tbl_items = tbl_items[:_lim_3_eff]
            _surplus   = max(0, _lim_3_eff - len(tbl_items)) if _lim_3_eff else 0
            hidden_tbl = total_tbl - len(tbl_items)
            tbl_lines  = []
            t_medals   = ["🥇", "🥈", "🥉"] + ["🎖️"] * 50
            for i, (name, data) in enumerate(tbl_items):
                t_credits = int(data["credits"])
                t_rank    = data.get("custom_rank") or get_rank(t_credits)
                t_display = strip_callsign(name) if strip_callsign_flag else name
                t_short   = t_display.replace('`', '') if len(t_display) <= 22 else t_display[:20].replace('`', '') + '..'
                t_medal   = data.get("custom_medal") or (t_medals[i] if i < len(t_medals) else "•")
                t_pts     = data.get("session_points", 0)
                t_hide    = data.get("hide_credits", False)
                t_hide_s  = data.get("hide_session", False)
                t_d_pts   = dp.get(name, 0)
                t_show_d  = has_daily and t_d_pts > 0
                def _tr(): return f"R: {t_credits:,}" if not t_hide else None
                def _ts(): return f"S: {t_pts:,}" if not t_hide_s and t_pts else None
                def _td(): return f"D: {t_d_pts:,}" if t_show_d else None
                def _t_tri(a, b, c):
                    parts = [p for p in [a, b, c] if p]
                    return f"({'  ·  '.join(parts)})" if parts else ""
                if tbl_key == "R":   t_pts_part = _t_tri(_tr(), _ts(), _td())
                elif tbl_key == "S": t_pts_part = _t_tri(_ts(), _tr(), _td())
                else:                t_pts_part = _t_tri(_td(), _tr(), _ts())
                tbl_lines.append(f"{t_medal} `{t_short}` — **{t_rank}** {t_pts_part}".rstrip())
                # Badge on this table if its key has highest priority among remaining tables
                # In 3x: badge goes on R if exists, else S, else D
                _order_keys_3x = {
                    "3R":  ("R", "S", "D"),
                    "3S":  ("S", "R", "D"),
                    "3D":  ("D", "R", "S"),
                    "3DS": ("D", "S", "R"),
                }
                _all_keys = _order_keys_3x.get(points_order, ())
                _priority = {"R": 0, "S": 1, "D": 2}
                # Badge key = highest priority key that has data
                _available = [k for k in ("R", "S", "D") if k != "D" or has_daily]
                _badge_tbl = min(_available, key=lambda k: _priority.get(k, 9)) if _available else None
                if show_punishment and tbl_key == _badge_tbl:
                    t_ucid = data.get("ucid")
                    t_pp   = data.get("hook_punishment") if "hook_punishment" in data else (pp.get(t_ucid, 0) if pp and t_ucid else 0)
                    t_badge = get_punishment_badge(t_pp, t_short, data.get("punishment_icon", ""), data.get("punishment_label", ""), data.get("punishment_pre_icon", ""))
                    if t_badge:
                        tbl_lines.append(t_badge)
            if hidden_tbl > 0:
                tbl_lines.append(f"*+ {hidden_tbl} more pilots*")
            if not tbl_lines:
                continue  # skip this table entirely if no data
            FIELD_LIMIT = 1020
            t_chunks, t_cur, t_len = [], [], 0
            for line in tbl_lines:
                ll = len(line) + 1
                if t_len + ll > FIELD_LIMIT and t_cur:
                    t_chunks.append("\n".join(t_cur))
                    t_cur, t_len = [line], ll
                else:
                    t_cur.append(line)
                    t_len += ll
            if t_cur:
                t_chunks.append("\n".join(t_cur))
            for i, chunk in enumerate(t_chunks):
                embed.add_field(
                    name=("\n" + tbl_title) if i == 0 else tbl_cont,
                    value=("\n" + chunk) if i == 0 else chunk,
                    inline=False
                )

    # Full-width separator — placed at the bottom to fix embed width
    # without interrupting the visual flow of the content.
    try:
        from core import utils as _dcssb_utils
        _ruler_name = _dcssb_utils.print_ruler(ruler_length=34)
    except Exception:
        _ruler_name = "─" * 34
    embed.add_field(name="\u200b", value=_ruler_name, inline=False)
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
        raw      = self.locals or {}
        interval = (raw.get("DEFAULT") or {}).get("update_interval", 300)
        self.updater.change_interval(seconds=int(interval))
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
            try:
                await node.write_file(ranks_file, ranks_data.encode("utf-8"))
            except Exception as e:
                self.log.error(f"FH_Report [{instance_name}]: failed to write Foothold_Ranks.lua: {e}")

        if camp_modified and persistence_file:
            try:
                await node.write_file(persistence_file, camp_data.encode("utf-8"))
            except Exception as e:
                self.log.error(f"FH_Report [{instance_name}]: failed to write campaign lua: {e}")

        # ── Write penalty state JSON ──────────────────────────────────────
        try:
            await node.write_file(
                penalty_file,
                json.dumps(penalty_state, indent=2, ensure_ascii=False).encode("utf-8")
            )
        except Exception as e:
            self.log.error(f"FH_Report [{instance_name}]: failed to write penalty state: {e}")

        # ── Append to log file ────────────────────────────────────────────
        if log_lines:
            try:
                existing = b""
                try:
                    existing = await node.read_file(log_file)
                except FileNotFoundError:
                    pass
                new_content = existing + "\n".join(log_lines).encode("utf-8") + b"\n"
                await node.write_file(log_file, new_content)
            except Exception as e:
                self.log.error(f"FH_Report [{instance_name}]: failed to write inactivity log: {e}")

    def _resolve_points_order(self, server_name: str, cfg: dict,
                              has_daily: bool = True,
                              has_session: bool = True) -> str:
        """Parse points_order — supports comma-separated cycle list.
        Skips daily-primary modes if has_daily=False and session-primary modes
        if has_session=False, advancing to the next valid mode in the cycle."""
        raw   = str(cfg.get("points_order") or "R").strip()
        items = [x.strip() for x in raw.split(",") if x.strip()]
        if not items:
            return "R"
        if len(items) == 1:
            return items[0]

        daily_primary   = {"D", "BD", "BDS", "2D", "2DS", "3D", "3DS"}
        session_primary = {"S", "BS", "2S", "3S"}

        # For compound modes (2x, 3x), define which data keys they use
        # Mode skips only if ALL its data keys have no data
        compound_keys = {
            "2R":  ("R", "S"),
            "2S":  ("S", "R"),
            "2D":  ("D", "R"),
            "2DS": ("D", "S"),
            "3R":  ("R", "S", "D"),
            "3S":  ("S", "R", "D"),
            "3D":  ("D", "R", "S"),
            "3DS": ("D", "S", "R"),
        }

        idx = self._cycle_index.get(server_name, 0)

        for _ in range(len(items)):
            candidate = items[idx % len(items)]
            idx += 1

            if candidate in compound_keys:
                # Compound mode: skip only if ALL tables have no data
                keys = compound_keys[candidate]
                has_any = any(
                    (k == "D" and has_daily) or
                    (k == "S" and has_session) or
                    (k == "R")
                    for k in keys
                )
                if not has_any:
                    continue
            else:
                # Simple/B mode: skip if primary key has no data
                if not has_daily and candidate in daily_primary:
                    continue
                if not has_session and candidate in session_primary:
                    continue

            self._cycle_index[server_name] = idx % len(items)
            return candidate

        # All modes skipped — fall back to R
        self._cycle_index[server_name] = idx % len(items)
        return "R"

    def _get_daily_file(self, saves_dir: str) -> str:
        """Return path to daily_points.json snapshot file."""
        return os.path.join(saves_dir, ".fhc", "daily_points.json")

    def _load_daily_snapshot(self, saves_dir: str) -> dict:
        """Load daily snapshot from disk. Returns dict with keys:
        'date' (YYYY-MM-DD), 'snapshot' {name: pts}, 'daily' {name: daily_pts}."""
        path = self._get_daily_file(saves_dir)
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    return json.load(f)
            except (ValueError, OSError):
                pass
        return {}

    def _save_daily_snapshot(self, saves_dir: str, data: dict) -> None:
        """Save daily snapshot to disk."""
        path = self._get_daily_file(saves_dir)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except OSError as e:
            self.log.error(f"FH_Report: failed to write daily snapshot: {e}")

    def _compute_daily_points(self, saves_dir: str, campaign_stats: dict,
                              reset_hour: int) -> dict:
        """Compute today's points for each player by comparing current campaign
        Points against the snapshot taken at reset_hour UTC.
        Returns dict {name: daily_pts} — only players with daily_pts > 0."""
        now_utc   = datetime.now(timezone.utc)
        today_str = now_utc.strftime("%Y-%m-%d")

        snap      = self._load_daily_snapshot(saves_dir)
        snap_date = snap.get("date", "")
        snapshot  = snap.get("snapshot", {})

        # Reset if:
        # - No snapshot exists yet (first run ever)
        # - Date changed and we're past reset_hour
        reset_time  = now_utc.replace(hour=reset_hour, minute=0, second=0, microsecond=0)
        first_run   = not snap_date
        needs_reset = first_run or (snap_date != today_str and now_utc >= reset_time)

        if needs_reset:
            if first_run:
                # First ever run — save empty snapshot so all current Points
                # count as daily from the start (baseline = 0 for everyone)
                snapshot = {}
            else:
                # Daily reset — save current Points as new baseline
                snapshot = dict(campaign_stats)
            self._save_daily_snapshot(saves_dir, {
                "date":     today_str,
                "snapshot": snapshot,
            })

        # Calculate daily delta for each player
        # New players not in snapshot get baseline=0 so all their Points count as daily
        daily = {}
        for name, current_pts in campaign_stats.items():
            baseline = snapshot.get(name, 0)
            delta    = max(0, current_pts - baseline)
            if delta > 0:
                daily[name] = delta

        return daily

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

        ranks_file    = os.path.join(saves_dir, "Foothold_Ranks.lua")
        ranks_missing = False
        try:
            await node.read_file(ranks_file)
        except FileNotFoundError:
            self.log.debug(
                f"FH_Report [{instance_name}]: Foothold_Ranks.lua not found — "
                f"showing zone status without leaderboard."
            )
            ranks_missing = True

        if not ranks_missing:
            # Deduplicate player entries caused by callsign changes before parsing.
            try:
                await deduplicate_ranks(ranks_file, persistence_file, node)
            except Exception as e:
                self.log.error(f"FH_Report [{instance_name}]: deduplication error: {e}")

        try:
            excluded_ucids = cfg.get("excluded_ucids") or []
            zones          = await parse_zones(persistence_file, node)
            players        = {} if ranks_missing else await parse_ranks(ranks_file, excluded_ucids, node)
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

        show_punishment   = _bool_cfg(cfg.get("show_punishment"))
        punishment_points = {}
        if show_punishment:
            punishment_points = await self._fetch_punishment_points()

        # Compute daily points first so we know if daily data exists
        # before resolving the points_order mode
        raw_order  = str(cfg.get("points_order") or "R").strip()
        daily_modes = {"D", "BD", "BDS", "2D", "2DS", "BR", "BS", "2R", "2S", "3R", "3S", "3D", "3DS"}
        needs_daily = any(m.strip() in daily_modes for m in raw_order.split(","))
        daily_pts: dict = {}
        if needs_daily and campaign_stats:
            reset_hour    = int(cfg.get("daily_reset_hour") or 0)
            # Override with day-specific hour if daily_reset_schedule is defined
            schedule      = cfg.get("daily_reset_schedule") or {}
            if schedule:
                day_keys  = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
                today_key = day_keys[datetime.now(timezone.utc).weekday()]
                if today_key in schedule:
                    reset_hour = int(schedule[today_key])
            daily_pts  = self._compute_daily_points(saves_dir, campaign_stats, reset_hour)

        # Detect if session data exists (any player with session_points > 0)
        has_session = any(d.get("session_points", 0) > 0 for d in players.values())

        # Resolve points_order — skips daily-primary modes if no daily data,
        # advancing to the next valid mode in the cycle instead of substituting
        current_order = self._resolve_points_order(
            instance_name, cfg,
            has_daily   = bool(daily_pts),
            has_session = has_session,
        )

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
            points_order        = current_order,
            bar_style_emoji     = _bool_cfg(cfg.get("bar_style_emoji")),
            daily_points        = daily_pts,
            max_pilots_3t       = int(cfg.get("max_pilots_3t") or 0) or None,
            show_pilot_card     = _bool_cfg(cfg.get("show_pilot_card")),
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
