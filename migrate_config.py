"""
FH_Report config migration script.
Called by install.bat when fh_report.yaml already exists.
Reads the existing config, adds any missing variables with their default values,
and warns about any obsolete variables found in server blocks.
"""
import sys
import os
import re


# ── Canonical header comment for fh_report.yaml ───────────────────────────────
HEADER_COMMENT = """# fh_report.yaml — FH_Report Plugin Configuration
# Place this file in: config/plugins/fh_report.yaml
#
# SERVER IDENTIFICATION:
#   Each server block key must match the DCSServerBot instance name as defined in nodes.yaml.
#   The plugin resolves the Foothold saves directory automatically, including in multi-node
#   cluster setups where DCS instances run on remote agent nodes.
#   If Foothold saves are in a non-standard location, override with saves_dir.
#
# REQUIRED per server:
#   channel_id     - Discord channel ID where the embed will be posted
#   campaign_name  - Name displayed in the embed title and footer
#
# OPTIONAL per server:
#   saves_dir      - Override Foothold saves path (default: auto-resolved from instance home)
#
# OPTIONAL - define in DEFAULT to apply to all servers,
#             or override per server block.
#
#   admin            - Who can query other players' stats with /fh_report player
#                      (default: "Admin"). Comma-separated list where each
#                      entry can be a Discord role name (as defined in DCSSB)
#                      or a specific username.
#                      Anyone not listed here can only view their own stats
#                      (Discord account must be linked via /linkme).
#                      Example:
#                        admin: Admin, SomeSpecificUser
#   update_interval  - Seconds between embed refreshes              (default: 300)
#   bar_length       - Number of squares in the progress bar        (default: 40)
#   bar_style_emoji  - Progress bar style                              (default: false)
#                      false = ANSI colored blocks (desktop/browser only)
#                      true  = emoji blocks 🟦🟥 (recommended for mobile compatibility)
#                              Note: emoji mode uses bar_length / 2 automatically
#   max_zones        - Max zones shown per column, omit = all       (default: 15)
#   zone_name_length - Max characters shown for zone names (8-24)   (default: 16)
#                      Values outside range are clamped automatically.
#   slot_status      - Show upgrade slot damage per zone            (default: false)
#                      false = show max level slots, all filled
#                      true  = show active vs destroyed status per zone
#                              Checks all upgrade slots and shows up to 5 symbols,
#                              prioritizing active slots over their position — so a
#                              zone with active slots beyond position 5 still shows
#                              them as active rather than appearing fully destroyed.
#                              🔹/🔺 = active unit slot  ◇/△ = destroyed slot
#   sort_zones_by_waypoint - Order active zones by their mission waypoint
#                      number instead of level/damage           (default: false)
#                      false = current behavior (sort by level, then active
#                              slot count)
#                      true  = BLUE zones: highest waypoint number first
#                              (descending). RED zones: lowest waypoint
#                              number first (ascending). Zones with no
#                              waypoint assigned fall to the end of the
#                              active block (same secondary order as
#                              false), before suspended zones — which are
#                              always last regardless of this setting.
#                      Waypoint numbers come from Foothold's in-memory
#                      WaypointList table (set from the .miz's trigger zone
#                      flavorText) — never written to any save file on its
#                      own, so this requires a one-time hot-injection dump
#                      to a shared cache file (saves_dir/.fhc/fhc_waypoints.lua,
#                      also used by FH_Control if installed). The dump is
#                      only triggered when that cache is missing, or when a
#                      campaign restart was just detected — never on every
#                      ordinary cycle, since the mapping is static for the
#                      life of a stable campaign. Requires the mission to
#                      be running at the time of the (re)trigger; falls
#                      back to the false behavior until a fresh cache is
#                      available.
#   strip_callsign   - Remove flight callsign prefix from pilot names (default: false)
#                      false = show names as-is
#                      true  = strip prefix. Squadron tags like [MA] are preserved.
#   points_order     - Controls leaderboard display and sort order  (default: R)
#                      Single table modes:
#                        R   = rank points only              (R: nnn)
#                        S   = session points only           (S: nnn)
#                        D   = daily points only             (D: nnn)
#                      Combined single table (B = all three values):
#                        BR  = sort by rank,    show R · S · D
#                        BS  = sort by session, show S · R · D
#                        BD  = sort by daily,   show D · R · S
#                        BDS = sort by daily,   show D · S · R
#                      Dual table (2 = two leaderboards):
#                        2R  = 1st by rank / 2nd by session
#                        2S  = 1st by session / 2nd by rank
#                        2D  = 1st by daily / 2nd by rank
#                        2DS = 1st by daily / 2nd by session
#                      Triple table (3 = three leaderboards):
#                        3R  = rank / session / daily
#                        3S  = session / rank / daily
#                        3D  = daily / rank / session
#                        3DS = daily / session / rank
#                      Quad table (4 = three leaderboards + Podium, see below):
#                        4R  = rank / session / Podium / daily
#                        4DS = daily / Podium / session / rank
#                      Podium-only table (no pilot leaderboard at all):
#                        P   = Podium only — see podium_days/podium_top below
#                      Comma-separated = cycle through modes on each update
#                      Example: points_order: 2S, BS, R
#                      D modes show nothing if no daily data yet (silently skipped)
#   compact_points   - In multi-table modes (2x, 3x), show only the primary data  (default: false)
#                      false = each table shows all data (R · S · D)
#                      true  = each table shows only its own sorted value (R, S, or D)
#   podium_days      - Window of days shown in the standalone "P" mode's
#                      Podium table                                  (default: 7)
#                      0 = all available history (since campaign start)
#                      A positive number = only the most recent N calendar
#                      dates that have at least one recorded closing event.
#                      Only affects points_order: P. The Podium icon is
#                      fixed (👑) everywhere and is not configurable.
#   podium_top       - Show the top N positions (1-50) for each closing
#                      event in the standalone "P" mode's Podium table
#                      (default: 1) — e.g. 3 shows 1st, 2nd AND 3rd place,
#                      not just 3rd place alone.
#                      Only affects points_order: P — see podium_days above.
#   podium_4x_days   - Same as podium_days, but for the Podium sub-block
#                      shown inside 4R/4DS instead of standalone "P"
#                      (default: 7). Independent from podium_days — the two
#                      Podium displays can be configured differently.
#   podium_4x_top    - Same as podium_top (top N positions, 1-50), but for
#                      4R/4DS's Podium sub-block (default: 1). Independent
#                      from podium_top.
#   podium_4x_min3_latest_day - Force at least the top 3 positions to show
#                      for the single most recent closing event(s) in
#                      4R/4DS's Podium sub-block, even if podium_4x_top is
#                      set lower (1 or 2)                        (default: false)
#                      false = every day strictly follows podium_4x_top
#                      true  = the most recent date always shows at least
#                              3 positions (both closures if that day had
#                              two); all other days still follow
#                              podium_4x_top exactly. Has no effect if
#                              podium_4x_top is already 3 or higher.
#                      Only affects 4R/4DS — the standalone "P" mode never
#                      uses this.
#   daily_reset_hour     - Hour (UTC) when daily points counter resets  (default: 0)
#                          Manual reset (no commands in this plugin): delete
#                          saves_dir/.fhc/daily_snapshot.json — the daily counter
#                          restarts cleanly at 0, it never retroactively counts
#                          everything accumulated up to that point.
#                          Campaign restart is also detected automatically: if both
#                          total points and total kills drop for common players,
#                          the daily snapshot resets on its own — no action needed.
#   daily_reset_schedule - Optional: override reset hour for specific days of the week.
#                          Only define the days that differ from daily_reset_hour.
#                          Days: mon, tue, wed, thu, fri, sat, sun
#                          Example: reset at midnight except Thursday and Saturday at 6am UTC:
#                            daily_reset_schedule:
#                              thu: 6
#                              sat: 6
#   max_pilots       - Max pilots shown in single-table modes (R,S,BR,BS) (default: all)
#   max_pilots_2t    - Max pilots per table in dual-table modes (2R,2S)   (default: all)
#                      Falls back to max_pilots if not set.
#   show_all_pilots  - Show all pilots beyond the field limit       (default: false)
#                      false = cut at limit, show "+ X more pilots"
#                      true  = split into multiple fields showing all pilots
#   show_pilot_card  - Show pilot career card below each pilot in the rank leaderboard
#                      (default: false). Requires Foothold v4.5 or later.
#                      Data sourced from Foothold_Ranks.lua — historical career totals only,
#                      not session or daily stats. Shown only on rank-ordered tables.
#                      Displays: fixed-wing hrs, helo hrs, kills, traps, refuels, deaths.
#                      Values of zero are omitted. If all values are zero the card is not shown.
#   pilot_card_icon  - Emoji shown at the start of the pilot career card line (default: 🔸)
#                      Example output:
#                        🥇 `Pilot1` — Colonel (R: 241,500)
#                        ·　🔸 129h fixed · 13h helo · 47 kills · 23 traps · 12 refuels · 3 deaths
#   show_session_card - Show session stats card below each pilot in the session leaderboard
#                      (default: false). Shown only on session-ordered tables.
#                      Data sourced from the campaign save file (playerStats) — current
#                      session kills and missions only, not career totals.
#                      "missions" counts only keys containing the word "mission"
#                      (CAP mission, SEAD mission, CAS mission, etc.)
#                      Displays up to 6 fields, priority order: missions, air, helo,
#                      SAM, ground, structure, infantry, deaths. Lowest-priority
#                      fields are dropped first if there are more than 6 with data.
#                      Deaths is always shown if > 0. Values of zero are omitted.
#                      If all values are zero the card is not shown.
#   session_card_icon - Emoji shown at the start of the session stats card line (default: 🔸)
#                      Mirrors the pilot career card categories (flight time, total kills,
#                      refuels, deaths), computed from session data (playerStats) using
#                      Foothold's confirmed session↔career field correlation. Session data
#                      has no fixed-wing/helicopter time split and no carrier traps
#                      equivalent — Foothold does not track these per-session.
#                      Example output:
#                        🥇 `Pilot1` — Staff Sergeant (S: 11,357)
#                        ·　🔸 4h flight · 41 kills · 3 refuels · 1 death
#   show_daily_card  - Show daily stats card below each pilot in the daily leaderboard
#                      (default: false). Shown only on daily-ordered tables.
#                      Same data and rules as show_session_card, but computed as the
#                      delta since the daily reset (daily_reset_hour / daily_reset_schedule)
#                      instead of full session totals.
#   daily_card_icon  - Emoji shown at the start of the daily stats card line (default: 🔸)
#                      Example output:
#                        🥇 `Pilot1` — Staff Sergeant (D: 890)
#                        ·　🔸 1h flight · 8 kills · 1 refuel
#   show_punishment  - Show punishment badges below sanctioned pilots (default: false)
#                      false = disabled
#                      true  = enabled (requires DCSServerBot punishment plugin)
#                      Reads from pu_events table. Thresholds:
#                      1pt 🧿 JAG's watch        11pt 🔍 JAG's investigation
#                      26pt ⚖️ JAG indictment    51pt ⛓️ Confined to quarters
#                      101pt 🔒 Brig time        200pt 💀 Dishonorably discharged
#   excluded_ucids   - UCIDs to hide from the leaderboard          (default: none)
#   disable_updates  - Silence this instance's embed entirely      (default: false)
#                      false = normal operation
#                      true  = this instance never reads, posts, or edits
#                              anything for this server — as if it weren't
#                              in the config at all. Useful when the same
#                              Foothold instance is reachable from more than
#                              one fh_report installation in the same cluster
#                              (e.g. one config per agent box) — set this to
#                              true on every duplicate copy except the one
#                              that should actually post.
#   show_player_cmd_hint - Show a reminder of /fh_report player in the embed
#                      footer                                        (default: true)
#                      false = disabled
#                      true  = adds a second line to the footer reminding
#                              players they can check their own stats.
#   player_cmd_hint_text - Customize the footer reminder text        (default:
#                      "Type /fh_report player to see your own stats.")
#
# ZONE DISPLAY NOTES:
#   - Neutral zones are counted in the progress bar as ⬜ but not listed.
#   - Suspended zones are shown fully filled at the bottom of each column.
#   - Hidden zones (name starts with "hidden") are fully ignored.
#
# UPGRADE SLOT INDICATORS (when slot_status: true):
#   🔹 = active BLUE slot   ◇ = destroyed BLUE slot
#   🔺 = active RED slot    △ = destroyed RED slot
"""

# ── All known valid variables ──────────────────────────────────────────────────
KNOWN_VARS = {
    "update_interval",
    "bar_length",
    "bar_style_emoji",
    "daily_reset_hour",
    "max_pilots_3t",
    "max_zones",
    "zone_name_length",
    "slot_status",
    "sort_zones_by_waypoint",
    "show_pilot_card",
    "pilot_card_icon",
    "show_session_card",
    "session_card_icon",
    "show_daily_card",
    "daily_card_icon",
    "show_punishment",
    "strip_callsign",
    "points_order",
    "compact_points",
    "show_all_pilots",
    "max_pilots",
    "max_pilots_2t",
    "excluded_ucids",
    "disable_updates",
    "saves_dir",
    "channel_id",
    "campaign_name",
    "admin",
    "show_player_cmd_hint",
    "player_cmd_hint_text",
    "podium_days",
    "podium_top",
    "podium_4x_days",
    "podium_4x_top",
    "podium_4x_min3_latest_day",
}

# ── Default values for DEFAULT block variables ─────────────────────────────────
DEFAULTS = {
    "admin":            "Admin",
    "update_interval":  300,
    "bar_length":       40,
    "bar_style_emoji":  False,
    "daily_reset_hour": 0,
    "max_zones":        15,
    "zone_name_length": 16,
    "slot_status":      False,
    "sort_zones_by_waypoint": False,
    "show_pilot_card":  False,
    "pilot_card_icon":  "🔸",
    "show_session_card": False,
    "session_card_icon": "🔸",
    "show_daily_card":  False,
    "daily_card_icon":  "🔸",
    "show_punishment":  False,
    "strip_callsign":   False,
    "points_order":     "R",
    "compact_points":   False,
    "show_all_pilots":  False,
    "show_player_cmd_hint":  True,
    "player_cmd_hint_text":  '"Type /fh_report player to see your own stats."',
    "podium_days":      7,
    "podium_top":       1,
    "podium_4x_days":      7,
    "podium_4x_top":       1,
    "podium_4x_min3_latest_day": False,
}

COMMENTS = {
    "admin":            "# Comma-separated Discord role name(s) and/or username(s)",
    "update_interval":  "# Seconds between embed refreshes",
    "bar_length":       "# Number of squares in the progress bar",
    "bar_style_emoji":  "# false = ANSI blocks (desktop only)  true = emoji blocks (mobile compatible)",
    "daily_reset_hour": "# Hour (UTC) when daily points reset (0 = midnight UTC)",
    "max_zones":        "# Max zones shown per column (omit for all)",
    "zone_name_length": "# Max chars for zone names (8-24, default 16)",
    "slot_status":      "# false = max level only  |  true = first 5 slots: active 🔹/🔺 vs destroyed ◇/△",
    "sort_zones_by_waypoint": "# false = sort by level/damage  |  true = sort by mission waypoint number",
    "show_pilot_card":  "# false = disabled  |  true = show career card per pilot (requires Foothold v4.5+)",
    "pilot_card_icon":  "# Emoji at the start of the pilot career card line (default: 🔸)",
    "show_session_card": "# false = disabled  |  true = show session stats card per pilot",
    "session_card_icon": "# Emoji at the start of the session stats card line (default: 🔸)",
    "show_daily_card":  "# false = disabled  |  true = show daily stats card per pilot",
    "daily_card_icon":  "# Emoji at the start of the daily stats card line (default: 🔸)",
    "show_punishment":  "# false = disabled  |  true = show punishment badges in leaderboard",
    "strip_callsign":   "",
    "points_order":     "",
    "show_all_pilots":  "# false = cut at limit  |  true = split into multiple fields",
    "show_player_cmd_hint": "# false = disabled  |  true = show /fh_report player reminder in footer",
    "player_cmd_hint_text": "# Text shown in the footer when show_player_cmd_hint is true",
    "podium_days":      "# 0 = since campaign start  |  N = last N days. Only affects points_order: P",
    "podium_top":       "# 1-50, shows the top N positions each day. Only affects points_order: P",
    "podium_4x_days":      "# Same as podium_days, but for 4R/4DS's Podium sub-block",
    "podium_4x_top":       "# Same as podium_top, but for 4R/4DS's Podium sub-block",
    "podium_4x_min3_latest_day": "# false = strictly follow podium_4x_top  |  true = force top 3 for the most recent day",
}


def main():
    if len(sys.argv) < 2:
        print("Usage: migrate_config.py <path_to_fh_report.yaml>")
        sys.exit(1)

    yaml_path = sys.argv[1]

    if not os.path.exists(yaml_path):
        print(f"File not found: {yaml_path}")
        sys.exit(1)

    with open(yaml_path, "r", encoding="utf-8") as f:
        content = f.read()

    added    = []
    obsolete = []

    # ── 1. Convert legacy 0/1 values to true/false for bool variables ──────────
    BOOL_VARS = {"bar_style_emoji", "slot_status", "strip_callsign", "sort_zones_by_waypoint",
                 "compact_points",
    "show_all_pilots", "show_punishment", "show_pilot_card", "compact_points",
    "show_session_card", "show_daily_card", "show_player_cmd_hint", "podium_4x_min3_latest_day"}
    bool_converted = []
    for bvar in BOOL_VARS:
        pattern = rf"(^\s+{bvar}\s*:\s*)(0|1)(\s*(?:#.*)?)$"
        def _replacer(m, bvar=bvar):
            val = "true" if m.group(2) == "1" else "false"
            bool_converted.append(f"  {bvar}: {m.group(2)} → {val}")
            return m.group(1) + val + m.group(3)
        content = re.sub(pattern, _replacer, content, flags=re.MULTILINE)
    if bool_converted:
        print("  Converted legacy 0/1 values to true/false:")
        for item in bool_converted:
            print(f"    {item}")

    # ── 2. Find DEFAULT block (after bool conversion) ──────────────────────────
    default_match = re.search(r"^DEFAULT:\s*\n((?:[ \t]+.*\n|#.*\n|\n)*)", content, re.MULTILINE)
    if not default_match:
        print("WARNING: No DEFAULT block found in config. Skipping migration.")
        sys.exit(0)

    default_block = default_match.group(1)

    # ── 3. Extract current user values from DEFAULT block ─────────────────────
    user_values = {}
    for key in DEFAULTS:
        m = re.search(rf"^\s+{re.escape(key)}\s*:\s*(.+?)(?:\s*#.*)?$", default_block, re.MULTILINE)
        if m:
            user_values[key] = m.group(1).strip()

    # ── 4. Detect missing variables ────────────────────────────────────────────
    for key in DEFAULTS:
        if key not in user_values:
            added.append(key)

    # ── 5. Reconstruct DEFAULT block in canonical order ────────────────────────
    new_default_lines = []
    for key, default_val in DEFAULTS.items():
        val     = user_values.get(key, default_val)
        comment = COMMENTS.get(key, "")
        # Format bool values as YAML true/false (lowercase)
        if isinstance(val, bool):
            val = "true" if val else "false"
        elif isinstance(default_val, bool) and str(val) in ("0", "1", "true", "false", "True", "False"):
            val = "true" if str(val) in ("1", "true", "True") else "false"
        new_default_lines.append(f"  {key}: {val}  {comment}\n")

    # Preserve extra lines (comments, and any non-DEFAULTS key blocks — including
    # multi-line YAML lists like 'excluded_ucids:' with '- item' lines
    # underneath). Each such block is captured as the key line plus every
    # following line indented deeper than it, so list items are never dropped.
    extra_lines = []
    _lines = default_block.splitlines(keepends=True)
    _i, _n = 0, len(_lines)
    while _i < _n:
        line = _lines[_i]
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            if not any(f"  {k}:" in line for k in DEFAULTS):
                extra_lines.append(line)
            _i += 1
            continue
        key_match = re.match(r"(\s*)(\w+)\s*:", line)
        if not key_match:
            # Orphan line (e.g. a list item under an already-consumed key) —
            # skip defensively rather than risk duplicating it.
            _i += 1
            continue
        indent   = len(key_match.group(1))
        key_name = key_match.group(2)
        if key_name in DEFAULTS:
            _i += 1
            continue
        # Collect this key line plus any deeper-indented continuation lines
        block_lines = [line]
        _j = _i + 1
        while _j < _n:
            nxt = _lines[_j]
            if not nxt.strip():
                break
            nxt_indent = len(nxt) - len(nxt.lstrip(" "))
            if nxt_indent > indent:
                block_lines.append(nxt)
                _j += 1
            else:
                break
        rest = line.split(":", 1)[1].strip()
        # Skip truly empty excluded_ucids (no items on this line or below)
        if key_name == "excluded_ucids" and len(block_lines) == 1 and (not rest or rest.startswith("#")):
            _i = _j
            continue
        extra_lines.extend(block_lines)
        _i = _j

    # ── 5c. Reposition 'daily_reset_schedule' right after 'daily_reset_hour' ───
    # It's a nested-dict value (per-day hour overrides) so it can't live in the
    # scalar DEFAULTS mechanism above — but it belongs visually right beneath
    # daily_reset_hour, not wherever extra_lines would otherwise place it.
    schedule_start = None
    for idx, l in enumerate(extra_lines):
        if re.match(r"^[ \t]*daily_reset_schedule\s*:", l):
            schedule_start = idx
            break
    if schedule_start is not None:
        base_indent  = len(extra_lines[schedule_start]) - len(extra_lines[schedule_start].lstrip(" "))
        schedule_end = schedule_start + 1
        while schedule_end < len(extra_lines):
            nxt = extra_lines[schedule_end]
            if not nxt.strip():
                break
            nxt_indent = len(nxt) - len(nxt.lstrip(" "))
            if nxt_indent > base_indent:
                schedule_end += 1
            else:
                break
        schedule_block = extra_lines[schedule_start:schedule_end]
        del extra_lines[schedule_start:schedule_end]
        insert_at = None
        for idx, l in enumerate(new_default_lines):
            if l.strip().startswith("daily_reset_hour:"):
                insert_at = idx + 1
                break
        if insert_at is not None:
            new_default_lines[insert_at:insert_at] = schedule_block
        else:
            # daily_reset_hour should always be in DEFAULTS, but fall back
            # to the original position rather than lose the block.
            extra_lines[schedule_start:schedule_start] = schedule_block

    new_default_block = "DEFAULT:\n" + "".join(new_default_lines)
    if extra_lines:
        while extra_lines and not extra_lines[-1].strip():
            extra_lines.pop()
        new_default_block += "".join(extra_lines) + "\n"

    # Replace old DEFAULT block (use positions from CURRENT content after bool conversion)
    content = content[:default_match.start()] + new_default_block + content[default_match.end():]

    # ── 6. Check server blocks for obsolete variables ──────────────────────────
    # Find all non-DEFAULT top-level blocks
    server_blocks = re.finditer(
        r'^"[^"]+"\s*:\s*\n((?:[ \t]+[^\n]*\n)*)',
        content, re.MULTILINE
    )
    for block_match in server_blocks:
        block_content = block_match.group(1)
        # Find all active (non-commented) variable keys in this block
        for var_match in re.finditer(r"^\s+([a-zA-Z_]+)\s*:", block_content, re.MULTILINE):
            var_name = var_match.group(1)
            if var_name not in KNOWN_VARS and var_name not in obsolete:
                obsolete.append(var_name)

    # ── 3. Update header comments ─────────────────────────────────────────────
    default_idx = content.find("\nDEFAULT:")
    if default_idx != -1:
        content = HEADER_COMMENT + "\n\nDEFAULT:" + content[default_idx + len("\nDEFAULT:"):]

    # ── 4. Save and report ─────────────────────────────────────────────────────
    with open(yaml_path, "w", encoding="utf-8") as f:
        f.write(content)
    if added:
        print(f"Migration complete. Added {len(added)} new variable(s) to DEFAULT:")
        for key in added:
            print(f"  + {key}: {DEFAULTS[key]}")
    else:
        print("Config is already up to date. No new variables needed.")
    print("  Header comments updated.")

    if obsolete:
        print()
        print("WARNING: The following variables were found in your server blocks")
        print("         but are no longer used in this version of FH_Report.")
        print("         They have no effect and can be safely removed or commented out:")
        for var in obsolete:
            print(f"  - {var}")

    sys.exit(0)


if __name__ == "__main__":
    main()
