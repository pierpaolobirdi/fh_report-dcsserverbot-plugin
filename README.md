# FH_Report Plugin for DCSServerBot

Automatically posts and updates a Discord embed with the current Foothold campaign status — front line progress, zone control, and pilot leaderboard — reading directly from the Foothold save files on disk.

---

## What it does

Every X seconds (configurable per server), the plugin reads the Foothold `.lua` save files and updates a single Discord embed in a configured channel with:

- **Progress bar** showing the balance of zone control between BLUE and RED
- **BLUE and RED zone columns** with upgrade level indicators, sorted by level
- **Pilot leaderboard** with each pilot's rank (based on Foothold credits) and points

The embed is always **edited in place** — it never spams new messages. Message IDs are stored in `plugins/fh_report/message_ids.json`. If that file is deleted, the plugin posts new messages and saves the new IDs.

The plugin supports **multiple server instances** — each server can have its own channel, saves directory, campaign name, and display settings.

---

## Compatibility

- Works with **any Foothold map** — the plugin reads `foothold.status` in the saves directory to identify the active persistence file automatically.
- Tested with **Foothold Persian Gulf** and **Foothold Sinai** on DCSServerBot v3.x.
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
   plugins:
     - fh_report
   ```

5. Restart DCSSB. The plugin will load and post the first embed automatically.

---

## Configuration

All configuration lives in `config/plugins/fh_report.yaml`.

### Structure

The yaml uses a `DEFAULT` section for shared settings and one named block per server instance. The name you give each server block is a **free alias** — it is only used internally by the plugin for logging and to store message IDs. It does **not** need to match any name in `servers.yaml` or anywhere else in DCSServerBot.

### DEFAULT section

Optional settings that apply to all servers unless overridden:

| Key | Default | Description |
|---|---|---|
| `update_interval` | `300` | Seconds between embed refreshes |
| `max_zones` | `14` | Max zones shown per column. Remainder shown as `+ X more bases` |
| `bar_length` | `20` | Number of squares in the progress bar |
| `max_pilots` | all | Max pilots shown in the leaderboard, ranked by credits |
| `excluded_ucids` | none | List of UCIDs to hide from the leaderboard |

### Per-server section

| Key | Required | Description |
|---|---|---|
| `saves_dir` | ✅ | Full path to the Foothold save files directory |
| `channel_id` | ✅ | Discord channel ID where the embed will be posted |
| `campaign_name` | ✅ | Name shown in the embed title and footer |
| `update_interval` | optional | Overrides DEFAULT value for this server |
| `bar_length` | optional | Overrides DEFAULT value for this server |
| `max_zones` | optional | Overrides DEFAULT value for this server |
| `max_pilots` | optional | Overrides DEFAULT value for this server |
| `excluded_ucids` | optional | Overrides DEFAULT value for this server |

### Example

```yaml
DEFAULT:
  update_interval: 300
  bar_length: 20
  max_zones: 14

"Foothold Server":
  saves_dir: "L:\\Saved Games\\DCS_Server\\Missions\\Saves"
  channel_id: 1458145804685541508
  campaign_name: "Operation Nova314 — Persian Gulf"
  excluded_ucids:
    - 71be6e5a09c1cd3979a8ec170fd99010   # Pier_Paolo

# "Second Server":
#   saves_dir: "L:\\Saved Games\\DCS_Server2\\Missions\\Saves"
#   channel_id: 1464645864684388402
#   campaign_name: "Operation Nova314 — Sinai"
#   update_interval: 120
```

---

## Pilot Ranks

Ranks are taken directly from the Foothold engine (`zoneCommander.lua`) and assigned based on accumulated credits:

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
