"""
FH_Report config migration script.
Called by install.bat when fh_report.yaml already exists.
Reads the existing config, adds any missing variables with their default values,
and warns about any obsolete variables found in server blocks.
"""
import sys
import os
import re

# ── All known valid variables ──────────────────────────────────────────────────
KNOWN_VARS = {
    "update_interval",
    "bar_length",
    "max_zones",
    "slot_status",
    "show_punishment",
    "strip_callsign",
    "show_all_pilots",
    "max_pilots",
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

    # ── 3. Save and report ─────────────────────────────────────────────────────
    if added:
        with open(yaml_path, "w", encoding="utf-8") as f:
            f.write(content)
        print(f"Migration complete. Added {len(added)} new variable(s) to DEFAULT:")
        for key in added:
            print(f"  + {key}: {DEFAULTS[key]}")
    else:
        print("Config is already up to date. No new variables needed.")

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
