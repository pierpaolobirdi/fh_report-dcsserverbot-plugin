# FH_Report Plugin for DCSServerBot

Automatically posts and updates a Discord embed with the current Foothold campaign status — front line progress, zone control, and pilot leaderboard — reading directly from the Foothold save files on disk.

---

## What it does

Every X seconds (configurable per server), the plugin reads the Foothold `.lua` save files and updates a single Discord embed in a configured channel with:

- **Progress bar** showing the balance of zone control between BLUE and RED, including neutral zones as ⬜
- **BLUE and RED zone columns** with upgrade level indicators, sorted by level and damage state
- **Pilot leaderboard** with each pilot's rank (based on Foothold credits) and points

The embed is always **edited in place** — it never spams new messages. Message IDs are stored in `plugins/fh_report/message_ids.json`. If that file is deleted, the plugin posts new messages and saves the new IDs.

The plugin supports **multiple server instances** — each server can have its own channel, saves directory, campaign name, and display settings.

---

## Zone display behaviour

| Zone type | Counted in bar | Listed in column | Position |
|---|---|---|---|
| Active BLUE/RED | ✅ | ✅ | Top, sorted by level and damage |
| Suspended BLUE/RED | ✅ | ✅ | Bottom of column, shown as fully filled |
| Neutral (side=0) | ✅ as ⬜ | ❌ | — |
| Hidden (name starts with `hidden`) | ❌ | ❌ | — |

**Suspended zones** are always shown as fully filled because Foothold reactivates them at full capacity. They appear at the bottom of their column to indicate they are far from the front line.

**Neutral zones** are counted in the progress bar as ⬜ squares but not listed, since they belong to neither faction.

---

## Upgrade slot display (`slot_status`)

Controlled by the `slot_status` config option:

**`slot_status: 0`** (default) — shows only the zone level, always fully filled:
```
Tunb Island AFB 🔹🔹🔹🔹🔹
Al Dahid        🔹🔹🔹🔹
```

**`slot_status: 1`** — shows active vs destroyed upgrade slots:
```
Tunb Island AFB 🔹🔹🔹🔹🔹   ← level 5, all active
Al Dahid        🔹◇◇◇◇       ← level 5, only 1 active
```

Slot indicators:
- `🔹` = active BLUE upgrade slot — `◇` = lost/empty BLUE slot
- `🔺` = active RED upgrade slot — `△` = lost/empty RED slot

Zone ordering with `slot_status: 1`:
1. Active zones sorted by level descending, then by active slots descending
2. Suspended zones at the bottom sorted by level descending

---

## Compatibility

- Works with **any Foothold map** — the plugin reads `foothold.status` in the saves directory to identify the active persistence file automatically.
- Requires **no database** — no tables are created.

---

## Installation

1. Copy the `fh_report/` folder into your DCSServerBot `plugins/` directory:
   ```
   DCSServerBot/
   └── plugins/
       └── fh_report/
           ├── __init__.py
           ├── commands.py
           ├── listener.py
           └── version.py
   ```

2. Copy `fh_report.yaml` into `config/plugins/`:
   ```
   DCSServerBot/
   └── config/
       └── plugins/
           └── fh_report.yaml
   ```

3. Edit `fh_report.yaml` with your values (see Configuration below).

4. Add `fh_report` to your plugin list in `main.yaml`:
   ```yaml
   opt_plugins:
     - fh_report
   ```

5. Restart DCSSB. The plugin will load and post the first embed automatically.

---

## Configuration

All configuration lives in `config/plugins/fh_report.yaml`.

### Structure

The yaml uses a `DEFAULT` section for shared settings and one named block per server instance. The name you give each server block is a **free alias** — it is only used internally by the plugin for logging and to store message IDs. It does **not** need to match any name in `servers.yaml` or anywhere else in DCSServerBot.

### DEFAULT section

| Key | Default | Description |
|---|---|---|
| `update_interval` | `300` | Seconds between embed refreshes |
| `max_zones` | `15` | Max zones shown per column. Remainder shown as `+ X more bases`. Omit for all |
| `bar_length` | `20` | Number of squares in the progress bar |
| `slot_status` | `0` | `0` = show max level only / `1` = show active vs lost slots |
| `show_punishment` | `0` | `0` = disabled / `1` = show punishment status below sanctioned pilots |
| `max_pilots` | all | Max pilots shown in the leaderboard |
| `excluded_ucids` | none | List of UCIDs to hide from the leaderboard |

### Per-server section

| Key | Required | Description |
|---|---|---|
| `saves_dir` | ✅ | Full path to the Foothold save files directory |
| `channel_id` | ✅ | Discord channel ID where the embed will be posted |
| `campaign_name` | ✅ | Name shown in the embed title and footer |
| `update_interval` | optional | Overrides DEFAULT |
| `bar_length` | optional | Overrides DEFAULT |
| `max_zones` | optional | Overrides DEFAULT |
| `max_pilots` | optional | Overrides DEFAULT |
| `slot_status` | optional | Overrides DEFAULT |
| `excluded_ucids` | optional | Overrides DEFAULT |

### Example

```yaml
DEFAULT:
  update_interval: 300
  bar_length: 20
  max_zones: 15
  slot_status: 0

"== Server-1 | Foothold ==":
  saves_dir: "C:\\Saved Games\\DCS_Server1\\Missions\\Saves"
  channel_id: 125536244541508
  campaign_name: "Operation — FootHoldMap"
  slot_status: 1        # override — show damage on this server
  excluded_ucids:
    - 71derf45ftgssr0f6744d99010   # User Pilot

#"== Server-2 | xxxx ==":
#  saves_dir: "C:\\Saved Games\\DCS_Server2\\Missions\\Saves"
#  channel_id: 125536244541508
#  campaign_name: "Operation — FootHoldMap"
```

---

## Pilot Ranks

Ranks are taken directly from the Foothold engine and assigned based on accumulated credits:

| Credits | Rank |
|---|---|
| 0 | Recruit |
| 3,000 | Aviator |
| 5,000 | Airman |
| 8,000 | Senior Airman |
| 12,000 | Staff Sergeant |
| 16,000 | Technical Sergeant |
| 22,000 | Master Sergeant |
| 30,000 | Senior Master Sergeant |
| 45,000 | Chief Master Sergeant |
| 65,000 | Second Lieutenant |
| 90,000 | First Lieutenant |

---

## Active map detection

The plugin reads `foothold.status` from the `saves_dir` to identify which persistence file is currently active. This file is written by Foothold and always points to the correct `.lua` for the running mission, regardless of which map is loaded. If `foothold.status` is not found, the plugin falls back to the most recently modified `foothold_*.lua` file.


---

## Pilot punishment status (`show_punishment`)

When `show_punishment: 1` is set, the plugin reads accumulated punishment points from the DCSServerBot `pu_events` database table and shows a status badge indented below each sanctioned pilot in the leaderboard:

```
🥇 Pier_Paolo — First Lieutenant (152,612)
🥈 Eskuvy — Technical Sergeant (19,765)
·　🔍 `Eskuvy` JAG's investigation 🔨🔨
🥉 Amirus — Staff Sergeant (14,639)
⭐ DOLAR — Recruit (1,034)
·　🔒 `DOLAR` Brig time 🔨🔨🔨🔨🔨
```

### Punishment thresholds

| Points | Icon | Status | Severity |
|---|---|---|---|
| 1 – 10 | ⚠️ | JAG's radar | 🔨 |
| 11 – 25 | 🔍 | JAG's investigation | 🔨🔨 |
| 26 – 50 | ⚖️ | JAG indictment filed | 🔨🔨🔨 |
| 51 – 100 | ⛓️ | Confined to quarters | 🔨🔨🔨🔨 |
| 101 – 200 | 🔒 | Brig time | 🔨🔨🔨🔨🔨 |
| 200+ | 💀 | Dishonorably discharged | 🔨🔨🔨🔨🔨🔨 |

### Requirements

- The **DCSServerBot Punishment plugin** must be installed and active
- If the punishment plugin is not active or the `pu_events` table does not exist, this option does nothing — no errors, no warnings

---

## Updating from a previous version

When you run `install.bat` on a system that already has FH_Report installed, it automatically detects the existing `fh_report.yaml` and runs `migrate_config.py` instead of overwriting it.

### What the migration does

- **Preserves** all your existing configuration — servers, channel IDs, campaign names, excluded UCIDs, and any custom values
- **Adds** any new variables introduced in the new version to the `DEFAULT` block, with their default values
- **Warns** if any variables in your server blocks are no longer used in the new version, so you can clean them up

### Example migration output

```
Existing fh_report.yaml found. Running migration...
Migration complete. Added 2 new variable(s) to DEFAULT:
  + max_zones: 15
  + slot_status: 0
```

Or if you have obsolete variables:

```
WARNING: The following variables were found in your server blocks
         but are no longer used in this version of FH_Report.
         They have no effect and can be safely removed or commented out:
  - old_variable
```

### Requirements

The migration script requires Python, which is always available in a DCSServerBot installation. The script automatically uses the DCSServerBot Python environment (`%USERPROFILE%\.dcssb\Scripts\python.exe`) or falls back to the system Python. If Python is not found, the existing config is left unchanged and a warning is shown.

---

## Files

| File | Purpose |
|---|---|
| `commands.py` | Main plugin logic — data parsing, embed building, Discord posting |
| `listener.py` | Placeholder event listener (required by DCSSB plugin structure) |
| `__init__.py` | Plugin registration |
| `version.py` | Version string |
| `fh_report.yaml` | Configuration (goes in `config/plugins/`) |
| `message_ids.json` | Auto-generated — stores Discord message IDs per server instance |

---

## Resetting the embed

To force the plugin to post a new message for a server (e.g. after moving it to a different channel), delete `plugins/fh_report/message_ids.json` and restart DCSSB. All servers will post new messages and save the new IDs.
