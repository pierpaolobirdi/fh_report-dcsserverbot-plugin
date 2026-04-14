# FH_Report Plugin for DCSServerBot

Automatically posts and updates a Discord embed with the current Foothold campaign status — front line progress, zone control, and pilot leaderboard — reading directly from the Foothold save files on disk. No database required.

---

## What it does

Every X seconds (configurable per server), the plugin reads the Foothold `.lua` save files and updates a single Discord embed in a configured channel with:

- **Progress bar** showing the balance of zone control between BLUE and RED, including neutral zones as ⬜
- **BLUE and RED zone columns** with upgrade level indicators, sorted by level and damage state
- **Pilot leaderboard** with each pilot's rank, session points, and optional punishment status

The embed is always **edited in place** — it never spams new messages. Message IDs are stored in `plugins/fh_report/message_ids.json`. If that file is deleted, the plugin posts new messages and saves the new IDs.

The plugin supports **multiple server instances** — each server can have its own channel, saves directory, campaign name, and display settings.

---

## Zone display

| Zone type | Counted in bar | Listed in column | Position |
|---|---|---|---|
| Active BLUE/RED | ✅ | ✅ | Top, sorted by level and damage |
| Suspended BLUE/RED | ✅ | ✅ | Bottom of column, shown as fully filled |
| Neutral (side=0) | ✅ as ⬜ | ❌ | — |
| Hidden (name starts with `hidden`) | ❌ | ❌ | — |

**Suspended zones** are shown as fully filled because Foothold reactivates them at full capacity. They appear at the bottom of their column.

**Neutral zones** are counted in the progress bar as ⬜ but not listed.

---

## Upgrade slot display (`slot_status`)

**`slot_status: 0`** (default) — shows zone level, always fully filled:
```
Bardufoss  🔹🔹🔹
Al Dahid   🔹🔹🔹🔹
```

**`slot_status: 1`** — shows active vs destroyed upgrade slots:
```
Bardufoss  🔹🔹🔹       ← level 3, all active
Al Dahid   🔹◇◇◇        ← level 4, only 1 active
```

- `🔹` = active BLUE slot · `◇` = lost BLUE slot
- `🔺` = active RED slot · `△` = lost RED slot

---

## Pilot leaderboard

### Points display (`points_order`)

The leaderboard can show rank points, session points, or both, in one or two tables:

| Value | Display | Sort |
|---|---|---|
| `R` | Rank points only `(R: nnn)` | By rank |
| `S` | Session points only `(S: nnn)` | By session |
| `BR` | Both `(R: nnn · S: nnn)` | By rank |
| `BS` | Both `(S: nnn · R: nnn)` | By session |
| `2R` | Two tables — first by rank, second by session | — |
| `2S` | Two tables — first by session, second by rank | — |

**Cyclic mode** — rotate through modes on each update by separating values with commas:
```yaml
points_order: R, 2R, S
```

**Rank points** come from `Foothold_Ranks.lua`. **Session points** come from `zonePersistance['playerStats']['Points']` in the active persistence file.

Tables sorted by session use `📊` in the title. Tables showing both values use a legend: `(R: Rank · S: Session)`.

### Callsign stripping (`strip_callsign`)

When `strip_callsign: 1`, flight callsign prefixes are removed from pilot names:
- `CALL 1-1 Pilot1` → `Pilot1`
- `CALL 1-3 | Pilot2` → `Pilot2`
- `[MA] Leka` → `[MA] Leka` (squadron tags preserved)

### Pilot limits

| Option | Applies to |
|---|---|
| `max_pilots` | Single-table modes: `R`, `S`, `BR`, `BS` |
| `max_pilots_2t` | Each table in dual-table modes: `2R`, `2S`. Falls back to `max_pilots` if omitted |

When the list is cut, `+ X more pilots` is shown at the bottom of the table.

`show_all_pilots: 1` splits the leaderboard into multiple Discord fields to show all pilots, with `🎖️ Leaderboard (cont.)` on continuation fields.

---

## Pilot punishment status (`show_punishment`)

When `show_punishment: 1`, the plugin reads accumulated punishment points from the DCSServerBot `pu_events` table and shows a badge below each sanctioned pilot:

```
🥇 `Eskuvy` — Technical Sergeant (R: 19,765)
🥈 `Amirus` — Staff Sergeant (R: 14,639)
·　🔍 `Amirus` JAG's investigation 🔨🔨
🥉 `Viper**` — Recruit (R: 2,626)
·　⚖️ `Viper**` JAG indictment filed 🔨🔨🔨
```

Punishment badges are always shown on the rank table. In session-only mode (`S`) they appear on the session table. In `2S` mode they appear on the second (rank) table.

### Punishment thresholds

| Points | Icon | Status | Severity |
|---|---|---|---|
| 1 – 10 | ⚠️ | JAG's radar | 🔨 |
| 11 – 25 | 🔍 | JAG's investigation | 🔨🔨 |
| 26 – 50 | ⚖️ | JAG indictment filed | 🔨🔨🔨 |
| 51 – 100 | ⛓️ | Confined to quarters | 🔨🔨🔨🔨 |
| 101 – 200 | 🔒 | Brig time | 🔨🔨🔨🔨🔨 |
| 200+ | 💀 | Dishonorably discharged | 🔨🔨🔨🔨🔨🔨 |

Requires the **DCSServerBot Punishment plugin** to be installed and active. If not active or the `pu_events` table does not exist, this option does nothing silently.

---

## Pilot ranks

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

The plugin reads `foothold.status` from `saves_dir` to identify the active persistence file. If not found, it falls back to the most recently modified `foothold_*.lua` file.

---

## Installation

### Fresh install


### Updating from a previous version

Run `install.bat` — it detects the existing config and runs `migrate_config.py`:
- **Preserves** all your existing configuration
- **Adds** new variables introduced in the new version with default values
- **Updates** header comments in your yaml with the latest documentation
- **Warns** about variables that are no longer used

```
Existing fh_report.yaml found. Running migration...
Migration complete. Added 2 new variable(s) to DEFAULT:
  + points_order: R
  + strip_callsign: 0
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
| `slot_status` | `0` | `0` = max level only / `1` = active vs lost slots |
| `strip_callsign` | `0` | `0` = names as-is / `1` = strip flight callsign prefixes |
| `points_order` | `R` | Leaderboard display and sort. See [Points display](#points-display-points_order) |
| `max_pilots` | all | Max pilots in single-table modes (`R`,`S`,`BR`,`BS`) |
| `max_pilots_2t` | all | Max pilots per table in dual-table modes (`2R`,`2S`). Falls back to `max_pilots` |
| `show_all_pilots` | `0` | `0` = cut at limit / `1` = split into multiple fields |
| `show_punishment` | `0` | `0` = disabled / `1` = show punishment badges |
| `excluded_ucids` | none | List of UCIDs to hide from the leaderboard |

### Example config

```yaml
DEFAULT:
  update_interval: 300
  bar_length: 20
  max_zones: 15
  slot_status: 1
  strip_callsign: 1
  points_order: R, 2R, S
  show_all_pilots: 0
  show_punishment: 1

"== Server-1 | Foothold ==":
  saves_dir: "C:\\Saved Games\\DCS_Server1\\Missions\\Saves"
  channel_id: 125536244541508
  campaign_name: "Operation Nova314 — FootHold"
  excluded_ucids:
    - 71derf45ftgssr0f6744d99010   # Pilot to hide
```

---

## Files

| File | Purpose |
|---|---|
| `commands.py` | Main plugin logic |
| `listener.py` | Placeholder event listener (required by DCSSB) |
| `__init__.py` | Plugin registration |
| `version.py` | Version string |
| `fh_report.yaml` | Configuration (goes in `config/plugins/`) |
| `migrate_config.py` | Migration script, called by `install.bat` on updates |
| `install.bat` | Installation and update script |
| `message_ids.json` | Auto-generated — stores Discord message IDs per server |

---

## Resetting the embed

Delete `plugins/fh_report/message_ids.json` and restart DCSServerBot to force new messages to be posted.

---

## Changelog

### v3.3.0
- Session leaderboard: `points_order` with `R` / `S` / `BR` / `BS` / `2R` / `2S`
- Cyclic mode: `points_order: R, 2R, S` rotates on each update interval
- `strip_callsign` removes flight callsign prefixes from pilot names
- `max_pilots_2t` for independent pilot limit per table in dual-table modes
- Punishment badges include pilot name, severity icon and escalating 🔨 indicators
- `migrate_config.py` updates header comments on every install/update

### v3.2.0
- `show_all_pilots` splits leaderboard into multiple fields when list is too long
- `+ X more pilots` shown when list is cut

### v3.1.0
- Pilot punishment status with `show_punishment`
- 6 severity levels from JAG's radar to Dishonorably discharged

### v3.0.0
- Multi-server support
- `slot_status` for upgrade slot damage display
- Neutral and suspended zone handling
- `foothold.status` for active map detection
