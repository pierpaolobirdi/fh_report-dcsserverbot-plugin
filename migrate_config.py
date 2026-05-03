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
#   The plugin uses this name to automatically resolve the Foothold saves directory:
#     {instance.home}\\Missions\\Saves
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
#   update_interval  - Seconds between embed refreshes              (default: 300)
#   bar_length       - Number of squares in the progress bar        (default: 20)
#   max_zones        - Max zones shown per column, omit = all       (default: 15)
#   zone_name_length - Max characters shown for zone names (8-24)   (default: 16)
#                      Values outside range are clamped automatically.
#   slot_status      - Show upgrade slot damage per zone            (default: 0)
#                      0 = show only max level (always fully filled)
#                      1 = show active vs lost slots
#   strip_callsign   - Remove flight callsign prefix from pilot names (default: 0)
#                      0 = show names as-is
#                      1 = strip prefix. Squadron tags like [MA] are preserved.
#   points_order     - Controls leaderboard display and sort order  (default: R)
#                      R, S, BR, BS, 2R, 2S or comma-separated to cycle
#   max_pilots       - Max pilots in single-table modes (R,S,BR,BS) (default: all)
#   max_pilots_2t    - Max pilots per table in dual-table modes (2R,2S) (default: all)
#   show_all_pilots  - 0 = cut at limit / 1 = split into multiple fields (default: 0)
#   show_punishment  - 0 = disabled / 1 = show punishment badges    (default: 0)
#   excluded_ucids   - UCIDs to hide from the leaderboard           (default: none)
#
# ZONE DISPLAY NOTES:
#   - Neutral zones are counted in the progress bar as ⬜ but not listed.
#   - Suspended zones are shown fully filled at the bottom of each column.
#   - Hidden zones (name starts with "hidden") are fully ignored.
#
# UPGRADE SLOT INDICATORS (when slot_status: 1):
#   🔹 = active BLUE slot   ◇ = lost/empty BLUE slot
#   🔺 = active RED slot    △ = lost/empty RED slot
"""

# ── All known valid variables ──────────────────────────────────────────────────
KNOWN_VARS = {
    "update_interval",
    "bar_length",
    "max_zones",
    "zone_name_length",
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
    "zone_name_length": 16,
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
    "zone_name_length": "# Max chars for zone names (8-24, default 16)",
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
