"""
fh_hook.py — Private FH_Report hook
This file is loaded automatically if present in the plugin directory.
It post-processes the players dict before the embed is built.

Keep this file and fh_hook.yaml PRIVATE — do not share or publish.
"""
import os
import random

try:
    import yaml
except ImportError:
    try:
        from ruamel.yaml import YAML as _RYAML
        class yaml:
            @staticmethod
            def safe_load(f):
                return _RYAML().load(f)
    except ImportError:
        yaml = None

# ── Load private config ───────────────────────────────────────────────────────
_HOOK_DIR    = os.path.dirname(os.path.abspath(__file__))
_CONFIG_FILE = os.path.join(_HOOK_DIR, "fh_hook.yaml")


def _load_config() -> dict:
    if not os.path.exists(_CONFIG_FILE) or yaml is None:
        return {}
    try:
        with open(_CONFIG_FILE, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def _apply_credits_mode(mode: str, real_credits: float, all_players: dict, ucid: str) -> float:
    """Calculate display credits based on mode, using real credits as base."""
    # Get sorted list of credits excluding this player
    others = sorted(
        [v["credits"] for k, v in all_players.items() if v.get("ucid") != ucid],
        reverse=True
    )

    if not others:
        return real_credits

    if mode == "first":
        # Slightly above current 1st
        return others[0] + real_credits * 0.001 + 1

    if mode == "last":
        # Slightly below current last
        return max(0, others[-1] - real_credits * 0.001 - 1)

    if mode == "top":
        # Average of top 3
        top3 = others[:3]
        base = sum(top3) / len(top3)
        return base + real_credits * 0.0005

    if mode == "mid":
        # Median of all
        mid_idx = len(others) // 2
        return others[mid_idx] + real_credits * 0.0001

    if mode == "bottom":
        # Average of bottom 3
        bottom3 = others[-3:]
        base = sum(bottom3) / len(bottom3)
        return max(0, base - real_credits * 0.0005)

    if str(mode).startswith("-"):
        # Position N (e.g. "-3" = 3rd place)
        try:
            pos = int(str(mode)[1:])  # 1-indexed position desired
            if pos <= 1:
                # Want 1st: above current 1st
                return others[0] + 1 if others else real_credits
            if pos > len(others):
                # Want last: below current last
                return max(0, others[-1] - 1) if others else 0
            # Want position N: slightly below the player currently at N-1
            # others is sorted desc, others[pos-2] is the player at position N-1
            above = others[pos - 2]  # player just above desired position
            below = others[pos - 1]  # player currently at desired position
            # Place just above the player currently at pos
            return below + (above - below) * 0.1 + 0.5
        except (ValueError, IndexError):
            return real_credits

    return real_credits


def _read_player_from_ranks(ranks_file: str, ucid: str) -> tuple[str | None, float]:
    """Read player name and credits directly from Foothold_Ranks.lua by UCID."""
    import re as _re
    try:
        with open(ranks_file, "r", encoding="utf-8") as f:
            content = f.read()
        # Find name from ucidToName
        ucid_pattern = _re.compile(
            r"\['" + _re.escape(ucid) + r"'\]=" + r'"([^"]+)"'
        )
        name_match = ucid_pattern.search(content)
        if not name_match:
            return None, 0.0
        name = name_match.group(1)
        # Find credits from players block
        block_pattern = _re.compile(
            r"\['" + _re.escape(name) + r"'\]=\{([^}]+)\}", _re.DOTALL
        )
        block_match = block_pattern.search(content)
        if not block_match:
            return name, 0.0
        credits_match = _re.search(r"\['credits'\]=([\d.]+)", block_match.group(1))
        credits = float(credits_match.group(1)) if credits_match else 0.0
        return name, credits
    except Exception:
        return None, 0.0


def post_process(players: dict, cfg: dict, server_name: str) -> dict:
    """
    Post-process the players dict.
    Called after parse_ranks, before build_embed.
    Players in excluded_ucids are re-added here if defined in fh_hook.yaml.

    players: dict of {name: {credits, ucid, ...}}
    cfg:     server config dict
    Returns: modified players dict
    """
    config = _load_config()
    hook_players = config.get("players", {})

    if not hook_players:
        return players

    # Get path to ranks file for reading excluded players
    saves_dir  = cfg.get("saves_dir", "")
    ranks_file = os.path.join(saves_dir, "Foothold_Ranks.lua") if saves_dir else ""

    for ucid, hook_cfg in hook_players.items():
        if not hook_cfg:
            hook_cfg = {}

        # Find player by ucid in current dict
        player_name = None
        player_data = None
        for name, data in players.items():
            if data.get("ucid") == ucid:
                player_name = name
                player_data = data.copy()
                break


        # If not found (excluded_ucids), read directly from ranks file
        if player_name is None and ranks_file and os.path.exists(ranks_file):
            real_name, real_credits = _read_player_from_ranks(ranks_file, ucid)
            if real_name:
                player_name = real_name
                player_data = {"credits": real_credits, "ucid": ucid}

        if player_name is None or player_data is None:
            continue

        real_credits = float(player_data.get("credits", 0))

        # ── display_name ──────────────────────────────────────────────────────
        display_name = hook_cfg.get("display_name")
        new_name = player_name if not display_name else str(display_name)

        # ── credits_fixed (priority over credits_mode) ────────────────────────
        credits_fixed = hook_cfg.get("credits_fixed")
        if credits_fixed is not None and credits_fixed is not False and credits_fixed != "":
            try:
                new_credits = float(credits_fixed)
            except (ValueError, TypeError):
                new_credits = real_credits
        else:
            # ── credits_mode ──────────────────────────────────────────────────
            credits_mode = str(hook_cfg.get("credits_mode") or "real").strip()
            if credits_mode == "real":
                new_credits = real_credits
            else:
                new_credits = _apply_credits_mode(credits_mode, real_credits, players, ucid)

        # ── rank ──────────────────────────────────────────────────────────────
        custom_rank = hook_cfg.get("rank")
        if custom_rank:
            player_data["custom_rank"] = str(custom_rank)
        elif "custom_rank" in player_data:
            del player_data["custom_rank"]

        # ── medal (custom emoji) ──────────────────────────────────────────────
        custom_medal = hook_cfg.get("medal")
        if custom_medal:
            player_data["custom_medal"] = str(custom_medal)

        # ── show_credits ──────────────────────────────────────────────────────
        show_credits = hook_cfg.get("show_credits")
        player_data["hide_credits"] = (show_credits is False)

        # ── punishment_points ─────────────────────────────────────────────────
        punishment_pts = hook_cfg.get("punishment_points")
        if punishment_pts is not None:
            try:
                player_data["hook_punishment"] = float(punishment_pts)
            except (ValueError, TypeError):
                pass

        # ── Apply changes ─────────────────────────────────────────────────────
        player_data["credits"] = new_credits

        # Remove old entry (only if it was in the dict — excluded players weren't)
        if player_name in players:
            del players[player_name]
        players[new_name] = player_data

    # Re-sort by credits
    return dict(sorted(players.items(), key=lambda x: x[1]["credits"], reverse=True))
