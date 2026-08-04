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
#   update_interval  - Seconds between embed refreshes              (default: 300)
#   bar_length       - Number of squares in the progress bar        (default: 40)
#   bar_style_emoji  - Progress bar style                              (default: 0)
#                      0 = ANSI colored blocks (desktop/browser only)
#                      1 = emoji blocks 🟦🟥 (recommended for mobile compatibility)
#                          Note: emoji mode uses bar_length / 2 automatically
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
#                      Comma-separated = cycle through modes on each update
#                      Example: points_order: 2S, BS, R
#                      D modes show nothing if no daily data yet (silently skipped)
#   daily_reset_hour     - Hour (UTC) when daily points counter resets  (default: 0)
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
#   show_all_pilots  - Show all pilots beyond the field limit       (default: 0)
#                      0 = cut at limit, show "+ X more pilots"
#                      1 = split into multiple fields showing all pilots
#   show_punishment  - Show punishment badges below sanctioned pilots (default: 0)
#                      0 = disabled
#                      1 = enabled (requires DCSServerBot punishment plugin)
#                      Reads from pu_events table. Thresholds:
#                      1pt 🧿 JAG's watch        11pt 🔍 JAG's investigation
#                      26pt ⚖️ JAG indictment    51pt ⛓️ Confined to quarters
#                      101pt 🔒 Brig time        200pt 💀 Dishonorably discharged
#   excluded_ucids   - UCIDs to hide from the leaderboard          (default: none)
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
    "bar_style_emoji",
    "daily_reset_hour",
    "max_pilots_3t",
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
    "bar_length": 40,
    "bar_style_emoji": 0,
    "daily_reset_hour": 0,
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
    "bar_style_emoji": "# 0 = ANSI blocks (desktop only)  1 = emoji blocks (mobile compatible)",
    "daily_reset_hour": "# Hour (UTC) when daily points reset (0 = midnight UTC)",
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

    # ── 1. Find DEFAULT block ──────────────────────────────────────────────────
    default_match = re.search(r"^DEFAULT:\s*\n((?:[ \t]+.*\n|#.*\n|\n)*)", content, re.MULTILINE)
    if not default_match:
        print("WARNING: No DEFAULT block found in config. Skipping migration.")
        sys.exit(0)

    default_block = default_match.group(1)

    # ── 2. Extract current user values from DEFAULT block ─────────────────────
    user_values = {}
    for key in DEFAULTS:
        m = re.search(rf"^\s+{re.escape(key)}\s*:\s*(.+?)(?:\s*#.*)?$", default_block, re.MULTILINE)
        if m:
            user_values[key] = m.group(1).strip()

    # ── 3. Detect missing variables ────────────────────────────────────────────
    for key in DEFAULTS:
        if key not in user_values:
            added.append(key)

    # ── 4. Reconstruct DEFAULT block in canonical order ────────────────────────
    new_default_lines = []
    for key, default_val in DEFAULTS.items():
        val     = user_values.get(key, default_val)
        comment = COMMENTS.get(key, "")
        new_default_lines.append(f"  {key}: {val}  {comment}\n")

    # Preserve any extra lines (comments, excluded_ucids, etc.) that are not
    # in DEFAULTS — keep them at the end of the DEFAULT block
    extra_lines = []
    for line in default_block.splitlines(keepends=True):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            # Keep comments and blank lines only if not already in canonical
            if not any(f"  {k}:" in line for k in DEFAULTS):
                extra_lines.append(line)
        else:
            # Active line — check if it belongs to a known key
            key_match = re.match(r"\s+(\w+)\s*:", line)
            if key_match and key_match.group(1) not in DEFAULTS:
                extra_lines.append(line)

    new_default_block = "DEFAULT:\n" + "".join(new_default_lines)
    if extra_lines:
        # Strip trailing blank lines from extras
        while extra_lines and not extra_lines[-1].strip():
            extra_lines.pop()
        new_default_block += "".join(extra_lines) + "\n"

    # Replace old DEFAULT block with reconstructed one
    content = content[:default_match.start()] + new_default_block + content[default_match.end():]

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
