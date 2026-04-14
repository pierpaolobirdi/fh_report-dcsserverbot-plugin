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
# SERVER INSTANCES:
#   Each server is defined by a unique name of your choice (e.g. "Foothold", "Server1").
#   This name is only used internally by the plugin — it does NOT need to match
#   any name in servers.yaml or anywhere else in DCSServerBot.
#   It is used to identify the server in logs and to store its Discord message ID.
#
# REQUIRED per server:
#   saves_dir      - Full path to the Foothold save files directory
#   channel_id     - Discord channel ID where the embed will be posted
#   campaign_name  - Name displayed in the embed title and footer
#
# OPTIONAL — define in DEFAULT to apply to all servers,
#             or per server to override the default value.
#
#   update_interval  - Seconds between embed refreshes              (default: 300)
#   max_zones        - Max zones shown per column, omit = all       (default: 15)
#   bar_length       - Number of squares in the progress bar        (default: 20)
#   max_pilots       - Max pilots shown in single-table modes (R,S,BR,BS) (default: all)
#   max_pilots_2t    - Max pilots per table in dual-table modes (2R,2S)   (default: all)
#                      If omitted, max_pilots applies to both tables
#   excluded_ucids   - UCIDs to hide from leaderboard               (default: none)
#   slot_status      - Show upgrade slot damage per zone            (default: 0)
#                      0 = show only max level (always fully filled)
#                      1 = show active vs lost slots (🔹🔹🔹◇◇ / 🔺🔺△△△)
#   strip_callsign   - Remove flight callsign prefix from pilot names (default: 0)
#                      0 = show names as-is
#                      1 = strip callsign prefix (e.g. "UZI 1-1 zarpa" -> "zarpa",
#                          "PONTIAC 1-3 | Asac" -> "Asac"). Squadron tags like [MA] preserved.
#   points_order     - Controls leaderboard display and sort order  (default: R)
#                      R   = show rank points only, sort by rank
#                      S   = show session points only, sort by session
#                      BR  = show both (R: nnn - S: nnn), sort by rank
#                      BS  = show both (S: nnn - R: nnn), sort by session
#                      2R  = two tables: first by rank, second by current session
#                      2S  = two tables: first by session, second by rank
#                      Comma-separated = cycle through modes on each update
#                      Example: points_order: R, 2R, S
#   show_all_pilots  - Show all pilots even if list exceeds field limit (default: 0)
#                      0 = cut at limit, show "+ X more pilots"
#                      1 = split into multiple fields showing all pilots
#   show_punishment  - Show punishment status below sanctioned pilots (default: 0)
#                      0 = disabled
#                      1 = enabled (requires DCSServerBot punishment plugin active)
#                      Reads from pu_events table. Thresholds:
#                      1pt JAG radar / 11pt JAG investigation / 26pt JAG indictment
#                      51pt Confined to quarters / 101pt Brig time / 200pt Discharged
#
# ZONE DISPLAY NOTES:
#   - Neutral zones (side=0) are counted in the progress bar as ⬜ but not listed.
#   - Suspended zones are always shown as fully filled and listed at the bottom
#     of each column. They suspend to save DCS resources but reactivate at full
#     capacity, so they are treated as complete.
#   - Hidden zones (name starts with "hidden") are fully ignored.
#
# UPGRADE SLOT INDICATORS (when slot_status: 1):
#   🔹 = active BLUE upgrade slot    ◇ = lost/empty BLUE slot
#   🔺 = active RED upgrade slot     △ = lost/empty RED slot
#   Example: 🔹🔹🔹◇◇ = level 5 zone with 3 active and 2 destroyed upgrades.
"""

# ── All known valid variables ──────────────────────────────────────────────────
KNOWN_VARS = {
    "update_interval",
    "bar_length",
    "max_zones",
    "slot_status",
    "show_punishment",
    "strip_callsign",
    "points_order",
    "show_all_pilots",
    "max_pilots",
    "max_pilots_2t",
    "excluded_ucids",
    "saves_dir",
    "channel_id",
    "campaign_name",
}

# ── Default values for DEFAULT block variables ─────────────────────────────────
DEFAULTS = {
    "update_interval": 300,
    "bar_length": 20,
    "max_zones": 15,
    "slot_status": 0,
    "show_punishment": 0,
    "strip_callsign": 0,
    "points_order": "R",
    "show_all_pilots": 0,
}

COMMENTS = {
    "update_interval": "# Seconds between embed refreshes",
    "bar_length":      "# Number of squares in the progress bar",
    "max_zones":       "# Max zones shown per column (omit for all)",
    "slot_status":     "# 0 = max level only  |  1 = show active vs lost slots",
    "show_punishment":  "# 0 = disabled  |  1 = show punishment badges in leaderboard",
    "show_all_pilots":      "# 0 = cut at limit show + X more  |  1 = split into multiple fields",
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

    added   = []
    obsolete = []

    # ── 1. Find DEFAULT block and add missing variables ────────────────────────
    default_match = re.search(r"^DEFAULT:\s*\n((?:[ \t]+.*\n|#.*\n|\n)*)", content, re.MULTILINE)
    if not default_match:
        print("WARNING: No DEFAULT block found in config. Skipping migration.")
        sys.exit(0)

    default_block = default_match.group(0)

    # Insert before trailing blank lines at end of DEFAULT block
    # so new variables appear inside the block, not after it
    block_end = default_match.end()
    trailing = re.search(r"(\n+)$", default_match.group(1))
    if trailing:
        insert_pos = block_end - len(trailing.group(1)) + 1
    else:
        insert_pos = block_end

    lines_to_add = []
    for key, default_val in DEFAULTS.items():
        pattern = rf"^\s*#?\s*{re.escape(key)}\s*:"
        if not re.search(pattern, default_block, re.MULTILINE):
            comment = COMMENTS.get(key, "")
            lines_to_add.append(f"  {key}: {default_val}  {comment}\n")
            added.append(key)

    if lines_to_add:
        insert_str = "".join(lines_to_add)
        content = content[:insert_pos] + insert_str + content[insert_pos:]

    # ── 2. Check server blocks for obsolete variables ──────────────────────────
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
        content = HEADER_COMMENT + content[default_idx + 1:]

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
