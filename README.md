# FH_Report — DCSServerBot Plugin

Automatically posts and keeps updated a Discord embed with the current Foothold campaign status — front line progress, zone control, and pilot leaderboard — reading directly from the Foothold save files. No database required.

Works on standalone single-node setups and on multi-node cluster setups (Master + Agent nodes) without any additional configuration.

---

## What it does

On a configurable interval the plugin reads the Foothold `.lua` save files and updates a single Discord embed in a configured channel with:

- **Progress bar** showing the balance of zone control between BLUE and RED, including neutral zones as ⬜
- **BLUE and RED zone columns** with zone levels and optional upgrade slot indicators, sorted by level and damage state
- **Pilot leaderboard** with each pilot's rank, session points, and optional punishment status

The embed is always **edited in place** — it never spams new messages. Message IDs are stored in `plugins/fh_report/message_ids.json`. If that file is deleted, the plugin posts fresh messages.

Multiple server instances are supported — each can have its own channel, campaign name, and display settings.

---

## Installation

### Requirements

- [DCSServerBot](https://github.com/Special-K-s-Flightsim-Bots/DCSServerBot) v3.x or later
- Foothold campaign active on at least one DCS instance

### Fresh install

1. Download the zip and extract it
2. Run `install.cmd` — it auto-detects your DCSServerBot installation
3. Edit `config/plugins/fh_report.yaml`:
   - Set each server block key to the **instance name** defined in your `nodes.yaml`
   - Set `channel_id` and `campaign_name` for each server
4. Restart DCSServerBot

### Updating from a previous version

Run `install.cmd` — it detects the existing config and runs `migrate_config.py` automatically:
- Preserves all your existing values
- Adds new variables with their defaults
- Updates header comments

```
Existing fh_report.yaml found. Running migration...
Migration complete. Added 2 new variable(s) to DEFAULT:
  + zone_name_length: 16
  + slot_status: 0
  Header comments updated.
```

---

## Configuration

All configuration lives in `config/plugins/fh_report.yaml`. The `DEFAULT` section applies to all servers and can be overridden per server block.

### Options reference

| Option | Default | Description |
|---|---|---|
| `update_interval` | `300` | Seconds between embed refreshes |
| `bar_length` | `20` | Number of squares in the progress bar |
| `max_zones` | `15` | Max zones per column. Omit for all |
| `zone_name_length` | `16` | Max characters for zone names (8–24, clamped) |
| `slot_status` | `0` | `0` = max level only / `1` = active vs lost slots |
| `strip_callsign` | `0` | `0` = names as-is / `1` = strip flight callsign prefix |
| `points_order` | `R` | Leaderboard mode. See [Leaderboard](#leaderboard) |
| `max_pilots` | all | Max pilots in single-table modes |
| `max_pilots_2t` | all | Max pilots per table in dual-table modes. Falls back to `max_pilots` |
| `show_all_pilots` | `0` | `0` = cut at limit / `1` = split into multiple fields |
| `show_punishment` | `0` | `0` = disabled / `1` = show punishment badges |
| `excluded_ucids` | none | List of UCIDs to hide from the leaderboard |
| `saves_dir` | auto | Override Foothold saves path. Only needed for non-standard locations |
| `persistence_file` | auto | (optional) Direct path to Foothold persistence `.lua` file. Takes priority over `saves_dir` and overrides `foothold.status` file content path (*support for virtual paths*) |

### Example config

```yaml
DEFAULT:
  update_interval: 300
  bar_length: 20
  max_zones: 15
  zone_name_length: 16
  slot_status: 1
  strip_callsign: 1
  points_order: 2S, BS, R
  show_all_pilots: 0
  show_punishment: 1

DCS_Server:                           # instance name from nodes.yaml
  channel_id: 1458145804685541508
  campaign_name: "Operation — FootHold"
  excluded_ucids:
    - e435a8583ad34583b7a709f58d98a6af

DCS_Server_2:
  channel_id: 1234567890123456789
  campaign_name: "Operation — FootHold 2"
```

---

## Zone display

| Zone type | Counted in bar | Listed in column |
|---|---|---|
| Active BLUE / RED | ✅ | ✅ Top, sorted by level and damage |
| Suspended BLUE / RED | ✅ | ✅ Bottom, shown as fully filled |
| Neutral (`side=0`) | ✅ as ⬜ | ❌ |
| Hidden (name starts with `hidden`) | ❌ | ❌ |

Suspended zones appear at the bottom of their column shown as fully filled — Foothold reactivates them at full capacity. Neutral zones are counted in the progress bar but not listed.

---

## Upgrade slot display (`slot_status`)

**`slot_status: 0`** (default) — shows zone level as fully filled:
```
Bardufoss    🔹🔹🔹
Kalixfors    🔹🔹🔹🔹
```

**`slot_status: 1`** — shows active vs destroyed upgrade slots:
```
Bardufoss    🔹◇◇     ← 3 slots total, only 1 active
Kalixfors    🔹🔹🔹🔹  ← 4 slots, all active
```

- `🔹` = active BLUE slot · `◇` = lost BLUE slot
- `🔺` = active RED slot · `△` = lost RED slot

---

## Leaderboard

### Points display (`points_order`)

| Value | Display | Sort |
|---|---|---|
| `R` | Rank points `(R: nnn)` | By rank |
| `S` | Session points `(S: nnn)` | By session |
| `BR` | Both `(R: nnn · S: nnn)` | By rank |
| `BS` | Both `(S: nnn · R: nnn)` | By session |
| `2R` | Two tables — rank / session | — |
| `2S` | Two tables — session / rank | — |

Comma-separated values cycle through modes on each update:
```yaml
points_order: 2S, BS, R
```

### Callsign stripping (`strip_callsign: 1`)

- `UZI 1-1 | Pilot1` → `Pilot1`
- `CALL 1-3 Pilot2` → `Pilot2`
- `[SQD] Pilot3` → `[SQD] Pilot3` ← squadron tags preserved

### Pilot ranks

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

## Punishment badges (`show_punishment: 1`)

Reads accumulated punishment points from the DCSServerBot Punishment plugin and shows a badge below each sanctioned pilot in the leaderboard:

```
🥇 `Pilot1` — Technical Sergeant (R: 19,765)
🥈 `Pilot2` — Staff Sergeant (R: 14,639)
·　🔍 `Pilot2` JAG's investigation (18 p.p.) 🔨🔨
🥉 `Pilot3` — Aviator (R: 2,626)
·　⚖️ `Pilot3` JAG indictment filed (32 p.p.) 🔨🔨🔨
```

| Points | Icon | Status | Severity |
|---|---|---|---|
| 1 – 10 | 🧿 | JAG's watch | 🔨 |
| 11 – 25 | 🔍 | JAG's investigation | 🔨🔨 |
| 26 – 50 | ⚖️ | JAG indictment filed | 🔨🔨🔨 |
| 51 – 100 | ⛓️ | Confined to quarters | 🔨🔨🔨🔨 |
| 101 – 200 | 🔒 | Brig time | 🔨🔨🔨🔨🔨 |
| 200+ | 💀 | Dishonorably discharged | 🔨🔨🔨🔨🔨🔨 |

Requires the DCSServerBot Punishment plugin. If not present or the `pu_events` table does not exist, the option does nothing silently.

---

## Files

| File | Purpose |
|---|---|
| `commands.py` | Main plugin logic |
| `listener.py` | Placeholder event listener (required by DCSSB) |
| `__init__.py` | Plugin registration |
| `version.py` | Version string |
| `fh_report.yaml` | Configuration template (goes in `config/plugins/`) |
| `migrate_config.py` | Migration script, called automatically by `install.cmd` on updates |
| `install.cmd` | Installation and update script |
| `message_ids.json` | Auto-generated — stores Discord message IDs per server |

---

## Resetting the embed

Delete `plugins/fh_report/message_ids.json` and restart DCSServerBot to force new messages to be posted.
