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
#   saves_dir           - Override Foothold saves path (default: auto-resolved from instance home)
#   commands_channel_id - Extra channel(s) (besides channel_id itself) where
#                         /fh_report player and /fh_report podium may be used
#                         for THIS server. Comma-separated list, or a YAML list.
#                         Two independent things, decided separately:
#                           - WHICH CHANNELS are allowed — governed purely by
#                             whether this is set for the server in question,
#                             regardless of how many servers you have
#                             configured overall:
#                               not set at all -> any channel is allowed
#                                                 (today's default)
#                               set            -> only channel_id itself, or
#                                                 one of these
#                           - WHETHER you must specify `server` on the
#                             command — only needed if you have MORE THAN
#                             ONE server configured at all (the channel is
#                             never used to guess between several, since two
#                             servers could end up sharing a
#                             commands_channel_id by mistake). With exactly
#                             one server configured, there's nothing to
#                             guess and `server` is never asked for.
#                         Example:
#                           commands_channel_id:
#                             - 1234567890123456789
#                             - 1234567890123456780
#

# OPTIONAL - define in DEFAULT to apply to all servers,
#             or override per server block. Listed below in the same order
#             they appear in the DEFAULT block.
#
#   admin            - Who can query other players' stats with /fh_report player
#                      (default: "Admin"). Comma-separated list where each
#                      entry can be a Discord role name (as defined in DCSSB)
#                      or a specific username.
#                      Anyone not listed here can only view their own stats
#                      (Discord account must be linked via /linkme).
#                      Example:
#                        admin: Admin, SomeSpecificUser
#   report_layout    - Which leaderboard tables to show, and in what order (default: R)
#                      A string built from these letters, used in any order/subset:
#                        D = Daily Leaderboard (today's points)
#                        P = Daily Podium (historical closing events)
#                        S = Session Leaderboard (current session)
#                        R = Pilot Leaderboard (by Rank)
#                      Examples:
#                        R        = just the Rank table (default)
#                        DS       = Daily table, then Session table
#                        DPS      = Daily, Podium, then Session (no Rank table)
#                        DPSR     = Daily, Podium, Session, Rank (all four)
#                      P only makes sense combined with at least one of D/S/R —
#                      "P" alone shows just the Podium, nothing else.
#                      Comma-separated = rotate between compositions, one step
#                      per update_interval, back to the first after the last —
#                      any number of groups is allowed. Example:
#                        report_layout: DP, SR
#                      shows "DP" one cycle, "SR" the next, "DP" again, and so
#                      on. Daily/Podium data is still kept up to date every
#                      cycle regardless of which group is currently showing.
#                      Rotation position isn't saved across a bot restart —
#                      it always starts at the first group again on load.
#                      points_detail_D/S/R (below) are NOT part of the
#                      rotation — they apply the same no matter which group
#                      is showing.
#                      The old points_order/compact_points values (from versions
#                      before v12.0.0) are NOT understood here any more — they
#                      are converted automatically, once, into report_layout/
#                      points_detail by the install/update process (which runs
#                      migrate_config.py against this file) — see CHANGELOG for
#                      the exact mapping used.
#   points_detail_D  - Exactly what the Daily table shows, in this order  (default: none)
#   points_detail_S  - Exactly what the Session table shows, in this order (default: none)
#   points_detail_R  - Exactly what the Rank table shows, in this order   (default: none)
#                      Opt-in, like every other show_*-style feature below —
#                      a table with no points_detail_<its own letter> key at
#                      all shows only its own value, same as the old
#                      compact_points: true. There's no points_detail_P —
#                      Podium has no per-player value to show.
#                      Each is a string of the letters D, S, R, in the order
#                      you want them shown — nothing is added automatically,
#                      not even the table's own letter, so if you want a
#                      table to show its own value it has to be in its own
#                      string explicitly.
#                      Example: report_layout: DPSR
#                        points_detail_D: DSR   → Daily table:   (D: nnn · S: nnn · R: nnn)
#                        points_detail_S: R     → Session table: (R: nnn)               — own S value omitted on purpose
#                        (points_detail_R not set) → Rank table: (R: nnn)               — own value only
#   update_interval  - Seconds between embed refreshes              (default: 300)
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
#   max_pilots       - Max pilots shown when report_layout has just 1 table (default: all)
#   max_pilots_2t    - Max pilots per table when report_layout has exactly
#                      2 tables (Podium doesn't count)                    (default: all)
#                      Falls back to max_pilots if not set.
#   max_pilots_3t    - Max pilots per table when report_layout has 3 or
#                      more tables (Podium doesn't count)                 (default: all)
#                      Falls back to max_pilots_2t, then max_pilots.
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
#   podium_days      - Window of days shown when report_layout is JUST "P"
#                      (Podium alone, no other table)                (default: 7)
#                      0 = all available history (since campaign start)
#                      A positive number = only the most recent N calendar
#                      dates that have at least one recorded closing event.
#                      Only affects report_layout: P on its own. The Podium
#                      icon is fixed (👑) everywhere and not configurable.
#   podium_top       - Show the top N positions (1-50) for each closing
#                      event when report_layout is JUST "P"
#                      (default: 1) — e.g. 3 shows 1st, 2nd AND 3rd place,
#                      not just 3rd place alone.
#                      Only affects report_layout: P on its own — see
#                      podium_days above.
#   podium_combined_days - Same as podium_days, but for the Podium table when
#                      "P" is combined with other letters in report_layout
#                      (e.g. DPSR, DPS, RPSD)                        (default: 7).
#                      Independent from podium_days — the two Podium
#                      displays can be configured differently.
#   podium_combined_top - Same as podium_top (top N positions, 1-50), but for
#                      "P" combined with other letters (default: 1).
#                      Independent from podium_top.
#   podium_combined_min3_latest_day - Force at least the top 3 positions to show
#                      for the single most recent closing event(s) when "P"
#                      is combined with other letters, even if
#                      podium_combined_top is set lower (1 or 2)  (default: false)
#                      false = every day strictly follows podium_combined_top
#                      true  = the most recent date always shows at least
#                              3 positions (both closures if that day had
#                              two); all other days still follow
#                              podium_combined_top exactly. Has no effect if
#                              podium_combined_top is already 3 or higher.
#                      Only affects "P" combined with other letters — "P" on
#                      its own never uses this.
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
    "admin",
    "report_layout",
    "points_detail_D",
    "points_detail_S",
    "points_detail_R",
    "update_interval",
    "daily_reset_hour",
    "daily_reset_schedule",
    "bar_length",
    "bar_style_emoji",
    "max_zones",
    "zone_name_length",
    "slot_status",
    "sort_zones_by_waypoint",
    "strip_callsign",
    "max_pilots",
    "max_pilots_2t",
    "max_pilots_3t",
    "show_all_pilots",
    "show_pilot_card",
    "pilot_card_icon",
    "show_session_card",
    "session_card_icon",
    "show_daily_card",
    "daily_card_icon",
    "podium_days",
    "podium_top",
    "podium_combined_days",
    "podium_combined_top",
    "podium_combined_min3_latest_day",
    "show_punishment",
    "excluded_ucids",
    "disable_updates",
    "show_player_cmd_hint",
    "player_cmd_hint_text",
    "saves_dir",
    "commands_channel_id",
    "channel_id",
    "campaign_name",
}

# ── Default values for DEFAULT block variables ─────────────────────────────────
# Dict order here IS the output order written to the DEFAULT block — keep it
# in sync with HEADER_COMMENT's order above.
DEFAULTS = {
    "admin":            "Admin",
    "report_layout":    "R",
    "update_interval":  300,
    "daily_reset_hour": 0,
    "bar_length":       40,
    "bar_style_emoji":  False,
    "max_zones":        15,
    "zone_name_length": 16,
    "slot_status":      False,
    "sort_zones_by_waypoint": False,
    "strip_callsign":   False,
    "show_all_pilots":  False,
    "show_pilot_card":  False,
    "pilot_card_icon":  "🔸",
    "show_session_card": False,
    "session_card_icon": "🔸",
    "show_daily_card":  False,
    "daily_card_icon":  "🔸",
    "podium_days":      7,
    "podium_top":       1,
    "podium_combined_days":      7,
    "podium_combined_top":       1,
    "podium_combined_min3_latest_day": False,
    "show_punishment":  False,
    "show_player_cmd_hint":  True,
    "player_cmd_hint_text":  '"Type /fh_report player to see your own stats."',
}

COMMENTS = {
    "admin":            "# Comma-separated Discord role name(s) and/or username(s)",
    "report_layout":    "# Letters D/P/S/R, any order/subset — see header",

    "update_interval":  "# Seconds between embed refreshes",
    "daily_reset_hour": "# Hour (UTC) when daily points reset (0 = midnight UTC)",
    "bar_length":       "# Number of squares in the progress bar",
    "bar_style_emoji":  "# false = ANSI blocks (desktop only)  true = emoji blocks (mobile compatible)",
    "max_zones":        "# Max zones shown per column (omit for all)",
    "zone_name_length": "# Max chars for zone names (8-24, default 16)",
    "slot_status":      "# false = max level only  |  true = first 5 slots: active 🔹/🔺 vs destroyed ◇/△",
    "sort_zones_by_waypoint": "# false = sort by level/damage  |  true = sort by mission waypoint number",
    "strip_callsign":   "",
    "show_all_pilots":  "# false = cut at limit  |  true = split into multiple fields",
    "show_pilot_card":  "# false = disabled  |  true = show career card per pilot (requires Foothold v4.5+)",
    "pilot_card_icon":  "# Emoji at the start of the pilot career card line (default: 🔸)",
    "show_session_card": "# false = disabled  |  true = show session stats card per pilot",
    "session_card_icon": "# Emoji at the start of the session stats card line (default: 🔸)",
    "show_daily_card":  "# false = disabled  |  true = show daily stats card per pilot",
    "daily_card_icon":  "# Emoji at the start of the daily stats card line (default: 🔸)",
    "podium_days":      "# 0 = since campaign start  |  N = last N days. Only affects report_layout: P alone",
    "podium_top":       "# 1-50, shows the top N positions each day. Only affects report_layout: P alone",
    "podium_combined_days":      "# Same as podium_days, but when \"P\" is combined with other letters",
    "podium_combined_top":       "# Same as podium_top, but when \"P\" is combined with other letters",
    "podium_combined_min3_latest_day": "# false = strictly follow podium_combined_top  |  true = force top 3 for the most recent day",
    "show_punishment":  "# false = disabled  |  true = show punishment badges in leaderboard",
    "show_player_cmd_hint": "# false = disabled  |  true = show /fh_report player reminder in footer",
    "player_cmd_hint_text": "# Text shown in the footer when show_player_cmd_hint is true",
}


# ── points_order -> report_layout/points_detail_<role> correspondence table ──
# Confirmed against the real rendering code, one legacy mode at a time, in
# chat. Each entry maps a legacy code to (report_layout, {role: detail}) —
# with one independent detail string PER TABLE ROLE now (not one shared
# string), this migration is exact for every single one of these 19 codes:
# the earlier shared-string design couldn't represent some of them exactly
# because different legacy modes disagreed on ordering for the same role
# (e.g. 2D wanted rank-before-session on its Daily table, 2DS wanted the
# opposite) — a per-role dict has no such conflict. "T" (the old code-level
# default when points_order was unset) is treated as "R": its old catch-all
# formatting was an unintentional quirk, not a real mode.
LEGACY_LAYOUT_MAP = {
    "T":   ("R",    {}),
    "R":   ("R",    {}),
    "S":   ("S",    {}),
    "D":   ("D",    {}),
    "P":   ("P",    {}),
    "BR":  ("R",    {"R": "RSD"}),
    "BS":  ("S",    {"S": "SRD"}),
    "BD":  ("D",    {"D": "DRS"}),
    "BDS": ("D",    {"D": "DSR"}),
    "2R":  ("RS",   {"R": "RSD", "S": "SRD"}),
    "2S":  ("SR",   {"S": "SRD", "R": "RSD"}),
    "2D":  ("DR",   {"D": "DRS", "R": "RSD"}),
    "2DS": ("DS",   {"D": "DSR", "S": "SRD"}),
    "3R":  ("RSD",  {"R": "RSD", "S": "SRD", "D": "DRS"}),
    "3S":  ("SRD",  {"S": "SRD", "R": "RSD", "D": "DRS"}),
    "3D":  ("DRS",  {"D": "DRS", "R": "RSD", "S": "SRD"}),
    "3DS": ("DSR",  {"D": "DSR", "S": "SRD", "R": "RSD"}),
    "4R":  ("RSPD", {"R": "RSD", "S": "SRD", "D": "DRS"}),
    "4DS": ("DPSR", {"D": "DSR", "S": "SRD", "R": "RSD"}),
}


def _translate_legacy_layout(code: str, compact: bool) -> tuple[str, dict]:
    """Translate one legacy points_order code (plus its paired
    compact_points flag) into (report_layout, {role: detail_string}).
    Falls back to treating an unrecognized code as an already-new-style
    layout string with no extra detail for any role, for anyone who typed
    the new grammar directly into the old points_order key before this
    migration ran. compact=True always yields an empty dict — no
    points_detail_<role> keys at all, i.e. every table shows only its own
    value, same as the old compact_points: true."""
    code = (code or "R").strip().upper()
    layout, detail_map = LEGACY_LAYOUT_MAP.get(code, (code, {}))
    if compact:
        return layout, {}
    return layout, dict(detail_map)


def _find_top_level_blocks(content: str) -> list[tuple[int, int]]:
    """Return (start, end) bounds for every top-level block in the file
    (DEFAULT: and every real, uncommented server block)."""
    top_level_re = re.compile(r'^\S.*$', re.MULTILINE)
    starts = [m.start() for m in top_level_re.finditer(content)]
    return [(s, (starts[i + 1] if i + 1 < len(starts) else len(content)))
            for i, s in enumerate(starts)]


def _find_kv_line(block_text: str, key: str):
    """Search block_text for an uncommented `key: value` line. Returns the
    match object (groups: indent, value, trailing comment) or None."""
    return re.search(
        rf'^([ \t]+){re.escape(key)}[ \t]*:[ \t]*([^\n#]*?)[ \t]*(#.*)?$',
        block_text, re.MULTILINE
    )


def rename_legacy_keys(content: str, renames: dict) -> tuple[str, list[str]]:
    """Straight key rename, value untouched — for cases where only the
    NAME changed, not its meaning (e.g. podium_4x_* -> podium_combined_*).
    Handles both live and commented-out (template example) occurrences
    anywhere in the file with one pass per key."""
    changes = []
    for old_name, new_name in renames.items():
        pattern = re.compile(rf'^([ \t]*#?[ \t]*){re.escape(old_name)}([ \t]*:)', re.MULTILINE)
        new_content, n = pattern.subn(rf'\1{new_name}\2', content)
        if n:
            changes.append(f"{old_name} -> {new_name} ({n} occurrence(s))")
        content = new_content
    return content, changes


def migrate_layout_variables(content: str) -> tuple[str, list[str]]:
    """Find every legacy layout-related key — `points_order` (+ its paired
    `compact_points`), and/or the older shared single-string
    `points_detail` from before the per-table split — anywhere in the
    file (DEFAULT or any server block), and rewrite them into
    report_layout + points_detail_D/points_detail_S/points_detail_R at
    the same spot, removing every legacy/intermediate key. If a block
    already defines report_layout explicitly, that value is respected —
    only the detail is (re)computed for whichever of its roles don't
    already have their own points_detail_<role> key. Returns
    (new_content, change_descriptions)."""
    changes = []
    edits = []  # (start, end, replacement text), applied back-to-front

    for block_start, block_end in _find_top_level_blocks(content):
        block_text = content[block_start:block_end]

        po_m = _find_kv_line(block_text, "points_order")
        pd_m = _find_kv_line(block_text, "points_detail")  # older shared single-string form
        if po_m is None and pd_m is None:
            continue  # nothing legacy in this block

        cp_m = _find_kv_line(block_text, "compact_points")
        rl_m = _find_kv_line(block_text, "report_layout")
        compact = bool(cp_m) and cp_m.group(2).strip().lower() in ("true", "1")

        if po_m is not None:
            po_layout, po_detail_map = _translate_legacy_layout(po_m.group(2).strip(), compact)
        else:
            po_layout, po_detail_map = None, {}

        final_layout = rl_m.group(2).strip() if rl_m is not None else po_layout
        if not final_layout:
            continue
        active_roles = [c for c in final_layout.upper() if c in "RSD"]

        # Which roles already have their OWN points_detail_<role> key in
        # this block? Those are left completely untouched.
        existing_detail_roles = {
            role for role in active_roles
            if _find_kv_line(block_text, f"points_detail_{role}") is not None
        }

        if pd_m is not None:
            # Older shared single-string points_detail — distribute it
            # per active role exactly as the old shared-string engine
            # actually behaved (own letter forced first, then whatever
            # else was in the string), so this specific migration step
            # is a byte-for-byte behavioural match of what was showing
            # before, for anyone who already adopted that intermediate
            # format.
            old_shared = pd_m.group(2).strip().strip('"').strip("'")
            if old_shared:
                detail_map = {
                    r: r + "".join(c for c in old_shared.upper() if c != r)
                    for r in active_roles if r not in existing_detail_roles
                }
            else:
                detail_map = {}
        else:
            detail_map = {r: v for r, v in po_detail_map.items()
                          if r in active_roles and r not in existing_detail_roles}

        new_lines = ""
        if rl_m is None:
            indent = (po_m or pd_m).group(1)
            new_lines += f"{indent}report_layout: {final_layout}\n"
        for role in ("D", "S", "R"):
            if role in detail_map:
                indent = (po_m or pd_m).group(1)
                new_lines += f'{indent}points_detail_{role}: "{detail_map[role]}"\n'

        summary_bits = []
        if rl_m is None:
            summary_bits.append(f"report_layout: {final_layout}")
        summary_bits += [f"points_detail_{r}: {v}" for r, v in detail_map.items()]
        changes.append(
            (f"points_order: {po_m.group(2).strip()} -> " if po_m else "points_detail (shared) -> ")
            + (", ".join(summary_bits) if summary_bits else "(removed, no replacement needed)")
        )

        # Replace points_order's line with the new lines (or, if there was
        # no points_order, replace the old shared points_detail's line).
        # Then separately delete compact_points and — if BOTH points_order
        # and an old shared points_detail existed together — the leftover
        # points_detail line too.
        primary_m = po_m if po_m is not None else pd_m
        edits.append((
            block_start + primary_m.start(),
            block_start + primary_m.end() + (1 if content[block_start + primary_m.end():block_start + primary_m.end() + 1] == "\n" else 0),
            new_lines
        ))
        for m in (cp_m, (pd_m if po_m is not None and pd_m is not None else None)):
            if m is None:
                continue
            m_start = block_start + m.start()
            m_end   = block_start + m.end()
            m_end   = m_end + 1 if content[m_end:m_end + 1] == "\n" else m_end
            edits.append((m_start, m_end, ""))

    edits.sort(key=lambda e: e[0], reverse=True)
    for start, end, repl in edits:
        content = content[:start] + repl + content[end:]

    return content, changes


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

    # ── 1b. Translate legacy points_order/compact_points (DEFAULT and any
    # server block) into report_layout/points_detail, wherever they appear.
    # Must run before the DEFAULT-block reconstruction below, so the newly
    # written report_layout/points_detail lines are picked up as regular
    # DEFAULTS-recognized keys with no further special-casing needed.
    content, layout_changes = migrate_layout_variables(content)
    if layout_changes:
        print("  Migrated legacy points_order/compact_points:")
        for item in layout_changes:
            print(f"    {item}")

    # ── 1c. Rename podium_4x_* -> podium_combined_* (name-only change, the
    # old name only made sense next to the retired 4R/4DS codes).
    content, podium_rename_changes = rename_legacy_keys(content, {
        "podium_4x_days": "podium_combined_days",
        "podium_4x_top": "podium_combined_top",
        "podium_4x_min3_latest_day": "podium_combined_min3_latest_day",
    })
    if podium_rename_changes:
        print("  Renamed:")
        for item in podium_rename_changes:
            print(f"    {item}")

    # Cosmetic: commented-out example lines (server-block templates showing
    # "#  points_order: R" for the admin to uncomment) aren't live values,
    # so migrate_layout_variables above correctly leaves them alone — but
    # they'd otherwise keep teaching the deprecated syntax forever. Refresh
    # just these two known example lines to the new grammar.
    commented_examples_updated = False
    new_content = re.sub(
        r'^#[ \t]*compact_points[ \t]*:[ \t]*false[ \t]*\n',
        '',
        content, flags=re.MULTILINE
    )
    if new_content != content:
        commented_examples_updated = True
    content = new_content
    new_content = re.sub(
        r'^(#[ \t]*)points_order[ \t]*:[ \t]*R[ \t]*$',
        r'\1report_layout: R\n#  points_detail_R: SRD',
        content, flags=re.MULTILINE
    )
    if new_content != content:
        commented_examples_updated = True
    content = new_content
    new_content = content.replace(
        "# options below only affect points_order: P",
        '# options below only affect report_layout: P alone'
    )
    if new_content != content:
        commented_examples_updated = True
    content = new_content
    new_content = content.replace(
        "# options below only affect 4R/4DS",
        '# options below only affect "P" combined w/ other letters'
    )
    if new_content != content:
        commented_examples_updated = True
    content = new_content
    if commented_examples_updated:
        print("  Refreshed commented-out example lines to the new syntax.")

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
        if re.match(r"^[ \t]*#?[ \t]*daily_reset_schedule\s*:", l):
            schedule_start = idx
            break
    def _visual_indent(line: str) -> int:
        # Strip at most one leading '#' (and its own indent) before
        # measuring indent, so a fully-commented block (every line
        # prefixed with '#') still nests correctly by the indentation of
        # what follows the '#', not the '#' character itself.
        stripped = line.rstrip("\n")
        lead = len(stripped) - len(stripped.lstrip(" "))
        rest = stripped[lead:]
        if rest.startswith("#"):
            rest = rest[1:]
            return lead + (len(rest) - len(rest.lstrip(" ")))
        return lead

    if schedule_start is not None:
        base_indent  = _visual_indent(extra_lines[schedule_start])
        schedule_end = schedule_start + 1
        while schedule_end < len(extra_lines):
            nxt = extra_lines[schedule_end]
            if not nxt.strip():
                break
            nxt_indent = _visual_indent(nxt)
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
