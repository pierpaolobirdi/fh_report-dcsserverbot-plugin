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

    zones = {"blue": [], "red": []}
    zone_names = re.findall(r"zonePersistance\['zones'\]\['([^']+)'\]", content)

    for zone in zone_names:
        pattern = rf"zonePersistance\['zones'\]\['{re.escape(zone)}'\] = \{{(.*?)(?=\nzonePersistance|\Z)"
        match = re.search(pattern, content, re.DOTALL)
        if not match:
            continue
        block = match.group(1)

        side_m  = re.search(r"\['side'\]=(\d+)", block)
        active_m = re.search(r"\['active'\]=(true|false)", block)
        level_m  = re.search(r"\['level'\]=(\d+)", block)

        if not side_m:
            continue

        side   = int(side_m.group(1))
        active = active_m.group(1) == "true" if active_m else False
        level  = int(level_m.group(1)) if level_m else 0

        if side == 0 or not active or level == 0:
            continue
        # Skip hidden/internal zones
        if zone.lower().startswith("hidden"):
            continue

        info = {"name": zone, "level": level}
        if side == 2:
            zones["blue"].append(info)
        elif side == 1:
            zones["red"].append(info)

    return zones


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
        players[clean_name] = {"credits": float(credit_m.group(1))}

    return dict(sorted(players.items(), key=lambda x: x[1]["credits"], reverse=True))


def build_embed(zones: dict, players: dict, campaign_name: str,
                max_zones: int | None, max_pilots: int | None,
                bar_length: int) -> discord.Embed:
    """Build the Discord embed from parsed Foothold data."""
    timestamp  = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    blue_count = len(zones["blue"])
    red_count  = len(zones["red"])
    total      = blue_count + red_count

    # Progress bar
    pct_blue  = round(blue_count / total * 100) if total > 0 else 50
    pct_red   = 100 - pct_blue
    blue_bars = round((blue_count / total) * bar_length) if total > 0 else bar_length // 2
    red_bars  = bar_length - blue_bars
    bar       = "🟦" * blue_bars + "🟥" * red_bars
    progress  = f"```\n{pct_blue}% {bar} {pct_red}%\n```"

    # BLUE zones
    blue_sorted = sorted(zones["blue"], key=lambda z: z["level"], reverse=True)
    limit       = max_zones if max_zones else len(blue_sorted)
    blue_lines  = []
    for z in blue_sorted[:limit]:
        stars = "🔹" * min(z["level"], 5)
        blue_lines.append(f"`{z['name']}` {stars}")
    if max_zones and len(blue_sorted) > max_zones:
        blue_lines.append(f"*+ {len(blue_sorted) - max_zones} more bases*")
    blue_lines.append(".")
    blue_text = "\n".join(blue_lines) if blue_lines else "—"

    # RED zones
    red_sorted = sorted(zones["red"], key=lambda z: z["level"], reverse=True)
    limit      = max_zones if max_zones else len(red_sorted)
    red_lines  = []
    for z in red_sorted[:limit]:
        stars = "🔺" * min(z["level"], 5)
        red_lines.append(f"`{z['name']}` {stars}")
    if max_zones and len(red_sorted) > max_zones:
        red_lines.append(f"*+ {len(red_sorted) - max_zones} more bases*")
    red_text = "\n".join(red_lines) if red_lines else "—"

    # Pilot leaderboard
    medals      = ["🥇", "🥈", "🥉"] + ["🎖️"] * 50
    pilot_items = list(players.items())
    if max_pilots:
        pilot_items = pilot_items[:max_pilots]
    pilot_lines = []
    for i, (name, data) in enumerate(pilot_items):
        credits = int(data["credits"])
        rank    = get_rank(credits)
        medal   = medals[i] if i < len(medals) else "•"
        short   = name.replace('`', '') if len(name) <= 22 else name[:20].replace('`', '') + '..'
        pilot_lines.append(f"{medal} `{short}` — **{rank}** ({credits:,} pts)")
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
    embed.add_field(
        name="\n🏆 __Pilot Leaderboard__",
        value="\n" + pilots_text[:1020],
        inline=False
    )
    embed.set_footer(text=f"{campaign_name} • Updated automatically")
    embed.timestamp = datetime.now(timezone.utc)

    return embed


# ── Plugin class ──────────────────────────────────────────────────────────────

# ── Plugin class ──────────────────────────────────────────────────────────────

class FHReport(Plugin):
    """DCSServerBot plugin — posts Foothold campaign status to Discord.
    Supports multiple server instances defined in fh_report.yaml."""

    def __init__(self, bot: DCSServerBot, eventlistener: Type[TEventListener] = None):
        super().__init__(bot, eventlistener)
        self._message_ids: dict = {}
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
            zones   = parse_zones(persistence_file)
            players = parse_ranks(ranks_file, excluded_ucids)
        except Exception as e:
            self.log.error(f"FH_Report [{server_name}]: error parsing data: {e}")
            return

        embed = build_embed(
            zones         = zones,
            players       = players,
            campaign_name = cfg.get("campaign_name", "Foothold Campaign"),
            max_zones     = cfg.get("max_zones", 14),
            max_pilots    = cfg.get("max_pilots") or None,
            bar_length    = int(cfg.get("bar_length") or 20),
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
