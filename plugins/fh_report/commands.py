"""
FH_Report Plugin for DCSServerBot
Reads Foothold campaign save files and posts/updates a Discord embed
with front-line status and pilot leaderboard. No database required.
"""

from __future__ import annotations

import glob
import logging
import os
import re
from datetime import datetime, timezone
from typing import Type

import discord
from discord.ext import tasks
from core import Plugin, TEventListener
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


def find_persistence_file(saves_dir: str) -> str | None:
    """Read the active Foothold persistence file path from foothold.status.
    Falls back to most recently modified foothold_*.lua if status file not found."""
    status_file = os.path.join(saves_dir, "foothold.status")
    if os.path.exists(status_file):
        with open(status_file, "r", encoding="utf-8") as f:
            path = f.read().strip().replace("/", os.sep)
        if os.path.exists(path):
            return path
    # Fallback
    candidates = glob.glob(os.path.join(saves_dir, "foothold_*.lua"))
    candidates = [f for f in candidates if "rank" not in os.path.basename(f).lower()]
    if not candidates:
        return None
    return max(candidates, key=os.path.getmtime)


def parse_zones(filepath: str) -> dict:
    """Parse zone persistence file. Returns {'blue': [...], 'red': [...]}."""
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()

    zones = {"blue": [], "red": [], "neutral": 0}
    zone_names = re.findall(r"zonePersistance\['zones'\]\['([^']+)'\]", content)

    for zone in zone_names:
        pattern = rf"zonePersistance\['zones'\]\['{re.escape(zone)}'\] = \{{(.*?)(?=\nzonePersistance|\Z)"
        match = re.search(pattern, content, re.DOTALL)
        if not match:
            continue
        block = match.group(1)

        side_m      = re.search(r"\['side'\]=(\d+)", block)
        active_m    = re.search(r"\['active'\]=(true|false)", block)
        level_m     = re.search(r"\['level'\]=(\d+)", block)
        suspended_m = re.search(r"\['suspended'\]=(true|false)", block)

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

        # Count active upgrade slots from remainingUnits
        ru_match = re.search(r"\['remainingUnits'\]=\{(.*?)\n  \},", block, re.DOTALL)
        active_slots = 0
        if ru_match:
            ru_block = ru_match.group(1)
            # Each top-level slot is [N]={ ... } — count those with content
            slot_matches = re.findall(r"\[(\d+)\]=\{([^}]*)\}", ru_block)
            active_slots = sum(1 for _, slot_content in slot_matches if slot_content.strip())
        if active_slots == 0 and not ru_match:
            active_slots = min(level, 5)

        info = {"name": zone, "level": min(level, 5), "active_slots": active_slots, "suspended": suspended}
        if side == 2:
            zones["blue"].append(info)
        elif side == 1:
            zones["red"].append(info)

    return zones


def parse_player_stats(filepath: str) -> dict:
    """Parse playerStats from Foothold persistence file.
    Returns dict {player_name: campaign_points}."""
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()
        stats_match = re.search(
            r"zonePersistance\['playerStats'\] = \{(.*?)^\}",
            content, re.DOTALL | re.MULTILINE
        )
        if not stats_match:
            return {}
        block = stats_match.group(1)
        results = {}
        for m in re.finditer(r"\['([^']+)'\]=\{([^}]+)\}", block, re.DOTALL):
            pts_m = re.search(r"\['Points'\]=(\d+)", m.group(2))
            if pts_m:
                results[m.group(1)] = int(pts_m.group(1))
        return results
    except Exception:
        return {}


def parse_ranks(filepath: str, excluded_ucids: list[str]) -> dict:
    """Parse Foothold_Ranks.lua. Returns pilot dict sorted by credits desc.
    Pilots whose UCID is in excluded_ucids are omitted."""
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()

    # Build set of excluded player names from ucidToName table
    excluded_names: set[str] = set()
    for ucid in excluded_ucids:
        m = re.search(rf"\['{re.escape(ucid)}'\]=\"([^\"]+)\"", content)
        if m:
            excluded_names.add(m.group(1))

    # Build name->ucid mapping from ucidToName table
    name_to_ucid = {}
    ucid_pattern = r"\['([a-f0-9]{32})'\]=\"([^\"]+)\""
    for ucid_m in re.finditer(ucid_pattern, content):
        name_to_ucid[ucid_m.group(2)] = ucid_m.group(1)

    players = {}
    block_pattern = r"\['([^']+)'\]=\{([^}]+)\}"
    for m in re.finditer(block_pattern, content):
        name  = m.group(1)
        block = m.group(2)
        credit_m = re.search(r"\['credits'\]=([\d.]+)", block)
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
    (1,   "⚠️", "JAG's radar",            1),
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
            return f"·　{used_pre} {prefix}{used_label} {gravity}"
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


def build_embed(zones: dict, players: dict, campaign_name: str,
                max_zones: int | None, max_pilots: int | None,
                bar_length: int, slot_status: int = 0,
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

    # Progress bar — neutrals shown as ⬜
    pct_blue     = round(blue_count / active_total * 100) if active_total > 0 else 50
    pct_red      = 100 - pct_blue
    blue_bars    = round((blue_count / total) * bar_length) if total > 0 else bar_length // 2
    neutral_bars = round((neutral_count / total) * bar_length) if total > 0 else 0
    red_bars     = bar_length - blue_bars - neutral_bars
    bar          = "🟦" * blue_bars + "⬜" * neutral_bars + "🟥" * red_bars
    progress     = f"```\n{pct_blue}% {bar} {pct_red}%\n```"

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
            active = min(z.get("active_slots", lvl), lvl)
            stars  = "🔹" * active + "◇" * (lvl - active)
        else:
            stars  = "🔹" * lvl
        blue_lines.append(f"`{z['name']}` {stars}")
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
            active = min(z.get("active_slots", lvl), lvl)
            stars  = "🔺" * active + "△" * (lvl - active)
        else:
            stars  = "🔺" * lvl
        red_lines.append(f"`{z['name']}` {stars}")
    if max_zones and len(red_sorted) > max_zones:
        red_lines.append(f"*+ {len(red_sorted) - max_zones} more bases*")
    red_text = "\n".join(red_lines) if red_lines else "—"

    # Pilot leaderboard — apply session stats and ordering
    cs = campaign_stats or {}

    # Add session_points to each player
    for name, data in players.items():
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
    show_session     = points_order in ("S", "BR", "BS", "2R", "2S")

    if order_by_session:
        pilot_items = sorted(players.items(), key=lambda x: x[1].get("session_points", 0), reverse=True)
    else:
        pilot_items = list(players.items())  # already sorted by total credits

    total_pilots_count = len(pilot_items)
    if max_pilots:
        pilot_items = pilot_items[:max_pilots]
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
        if hide_credits:
            pts_str = ""
        elif points_order == "R":
            pts_str = f"(R: {credits:,})"
        elif points_order == "S":
            pts_str = f"(S: {s_pts:,})"
        elif points_order == "BR":
            pts_str = f"(R: {credits:,} · S: {s_pts:,})" if s_pts else f"(R: {credits:,})"
        elif points_order == "BS":
            pts_str = f"(S: {s_pts:,} · R: {credits:,})" if s_pts else f"(R: {credits:,})"
        elif points_order == "2R":
            pts_str = f"(R: {credits:,})"
        else:  # 2S
            pts_str = f"(S: {s_pts:,})" if s_pts else "(S: 0)"

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
    if points_order in ("2R", "2S") and cs:
        # For 2R: second table = session. For 2S: second table = rank (already sorted)
        if points_order == "2R":
            second_items = sorted(players.items(), key=lambda x: x[1].get("session_points", 0), reverse=True)
            second_items = [item for item in second_items if item[1].get("session_points", 0) > 0]
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
                if s_hide:
                    line = f"{s_medal} `{s_short}` — **{s_rank}**"
                elif points_order == "2R":
                    line = f"{s_medal} `{s_short}` — **{s_rank}** (S: {s_pts:,})"
                else:
                    line = f"{s_medal} `{s_short}` — **{s_rank}** (R: {s_credits:,})"
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


# ── Plugin class ──────────────────────────────────────────────────────────────

class FHReport(Plugin):
    """DCSServerBot plugin — posts Foothold campaign status to Discord.
    Supports multiple server instances defined in fh_report.yaml."""

    def __init__(self, bot: DCSServerBot, eventlistener: Type[TEventListener] = None):
        super().__init__(bot, eventlistener)
        self._message_ids: dict = {}
        self._cycle_index: dict = {}  # tracks points_order cycle position per server
        self._message_ids_file: str = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "message_ids.json"
        )
        self._servers: dict = {}
        self._default_cfg: dict = {}

    # ── Lifecycle ─────────────────────────────────────────────────────────

    async def install(self) -> None:
        """No database tables needed for this plugin."""
        pass

    async def cog_load(self) -> None:
        await super().cog_load()
        # Use self.locals to access the raw yaml before DCSSB processes DEFAULT
        raw = self.locals or {}
        self._default_cfg = raw.get("DEFAULT") or {}
        self._message_ids = self._load_message_ids()

        # Build per-server configs merging DEFAULT values
        for name, srv_cfg in raw.items():
            if name == "DEFAULT" or not isinstance(srv_cfg, dict):
                continue
            merged = dict(self._default_cfg)
            merged.update(srv_cfg)
            self._servers[name] = merged

        if not self._servers:
            self.log.warning("FH_Report: no servers configured.")

    async def cog_unload(self) -> None:
        if self.updater.is_running():
            self.updater.cancel()
        await super().cog_unload()

    async def on_ready(self) -> None:
        await super().on_ready()
        if not self._servers:
            return
        interval = min(
            int(cfg.get("update_interval") or 300)
            for cfg in self._servers.values()
        )
        self.updater.change_interval(seconds=interval)
        if not self.updater.is_running():
            self.updater.start()

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
        for server_name, cfg in self._servers.items():
            try:
                await self._update_server(server_name, cfg)
            except Exception as e:
                self.log.error(f"FH_Report [{server_name}]: unexpected error: {e}", exc_info=True)

    def _resolve_points_order(self, server_name: str, cfg: dict) -> str:
        """Parse points_order from config — supports comma-separated cycle list.
        Returns the current value and advances the cycle index for next call."""
        raw = str(cfg.get("points_order") or "R").strip()
        # Parse comma-separated list
        items = [x.strip() for x in raw.split(",") if x.strip()]
        if len(items) <= 1:
            return items[0] if items else "R"
        # Cycle through items
        idx = self._cycle_index.get(server_name, 0)
        value = items[idx % len(items)]
        self._cycle_index[server_name] = (idx + 1) % len(items)
        return value

    async def _fetch_punishment_points(self) -> dict:
        """Fetch total punishment points per UCID from pu_events table.
        Returns empty dict if table doesn't exist or any error occurs."""
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

    async def _update_server(self, server_name: str, cfg: dict):
        channel_id = cfg.get("channel_id")
        saves_dir  = cfg.get("saves_dir")
        if not channel_id or not saves_dir:
            self.log.warning(f"FH_Report [{server_name}]: channel_id or saves_dir not configured.")
            return

        channel = self.bot.get_channel(int(channel_id))
        if not channel:
            self.log.warning(f"FH_Report [{server_name}]: channel {channel_id} not found.")
            return

        persistence_file = find_persistence_file(saves_dir)
        if not persistence_file:
            self.log.warning(f"FH_Report [{server_name}]: no foothold_*.lua found in {saves_dir}")
            return

        ranks_file = os.path.join(saves_dir, "Foothold_Ranks.lua")
        if not os.path.exists(ranks_file):
            self.log.warning(f"FH_Report [{server_name}]: Foothold_Ranks.lua not found in {saves_dir}")
            return

        try:
            excluded_ucids = cfg.get("excluded_ucids") or []
            zones          = parse_zones(persistence_file)
            players        = parse_ranks(ranks_file, excluded_ucids)
            campaign_stats = parse_player_stats(persistence_file)
        except Exception as e:
            self.log.error(f"FH_Report [{server_name}]: error parsing data: {e}")
            return

        # ── Optional private hook — post-processes players dict ───────────────
        if _HAS_HOOK:
            try:
                players = _fh_hook.post_process(players, cfg, server_name)
            except Exception:
                pass  # Hook errors are silently ignored

        # Fetch punishment points if enabled
        show_punishment = int(cfg.get("show_punishment") or 0)
        punishment_points = {}
        if show_punishment:
            punishment_points = await self._fetch_punishment_points()

        embed = build_embed(
            zones             = zones,
            players           = players,
            campaign_name     = cfg.get("campaign_name", "Foothold Campaign"),
            max_zones         = cfg.get("max_zones") or None,
            max_pilots        = cfg.get("max_pilots") or None,
            bar_length        = int(cfg.get("bar_length") or 20),
            slot_status       = int(cfg.get("slot_status") or 0),
            punishment_points = punishment_points,
            show_punishment   = show_punishment,
            show_all_pilots     = int(cfg.get("show_all_pilots") or 0),
            strip_callsign_flag = int(cfg.get("strip_callsign") or 0),
            max_pilots_2t       = cfg.get("max_pilots_2t") or None,
            campaign_stats      = campaign_stats,
            points_order        = self._resolve_points_order(server_name, cfg),
        )

        try:
            msg_id = self._message_ids.get(server_name)
            if msg_id:
                try:
                    msg = await channel.fetch_message(msg_id)
                    await msg.edit(embed=embed)
                    return
                except discord.NotFound:
                    self.log.warning(f"FH_Report [{server_name}]: previous message not found, posting new one.")
                    self._message_ids.pop(server_name, None)

            msg = await channel.send(embed=embed)
            self._message_ids[server_name] = msg.id
            self._save_message_ids()

        except discord.HTTPException as e:
            self.log.error(f"FH_Report [{server_name}]: Discord error: {e}")


async def setup(bot: DCSServerBot):
    await bot.add_cog(FHReport(bot))
