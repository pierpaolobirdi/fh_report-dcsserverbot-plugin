# FH_Report — DCSServerBot Plugin

Automatically posts and keeps updated a Discord embed with the current Foothold campaign status — front line progress, zone control, and pilot leaderboard — reading directly from the Foothold save files. No database required.

Works on standalone single-node setups and on multi-node cluster setups (Master + Agent nodes) without any additional configuration.

---

## What it does

On a configurable interval the plugin reads the Foothold `.lua` save files and updates a single Discord embed in a configured channel with:

- **Progress bar** showing the balance of zone control between BLUE and RED
- **BLUE and RED zone columns** with zone levels and optional upgrade slot indicators, sorted by level and damage state
- **Pilot leaderboard** with rank, session, daily points, optional career card, and optional punishment status

The embed is always **edited in place** — it never spams new messages.

Multiple server instances are supported — each can have its own channel, campaign name, and display settings.

---

## Requirements

- [DCSServerBot](https://github.com/Special-K-s-Flightsim-Bots/DCSServerBot) v3.x or later (by Special K)
- [Foothold campaign](https://github.com/leka1986/Lekas-Foothold) (by Leka) active on at least one DCS instance

---

## Installation

### Fresh install

1. Download the zip and extract it
2. Run `install.cmd` — it auto-detects your DCSServerBot installation
3. Edit `config/plugins/fh_report.yaml`:
   - Set each server block key to the **instance name** defined in your `nodes.yaml`
   - Set `channel_id` and `campaign_name` for each server
4. Restart DCSServerBot

### Updating from a previous version

Run `install.cmd` — it detects the existing config and migrates it automatically:
- Preserves all your existing values
- Adds new variables with their defaults
- Updates header comments

---

## Configuration

All configuration lives in `config/plugins/fh_report.yaml`. The `DEFAULT` section applies to all servers and can be overridden per server block.

### Options reference

| Option | Default | Description |
|---|---|---|
| `update_interval` | `300` | Seconds between embed refreshes |
| `bar_length` | `40` | Number of squares in the progress bar |
| `bar_style_emoji` | `false` | `true` = emoji bar 🟦🟥 (recommended for mobile) |
| `max_zones` | `15` | Max zones per column. Omit for all |
| `zone_name_length` | `16` | Max characters for zone names (8–24, clamped) |
| `slot_status` | `false` | `true` = show active vs destroyed upgrade slots |
| `strip_callsign` | `false` | `true` = strip flight callsign prefix from pilot names |
| `points_order` | `R` | Leaderboard mode — see [Leaderboard](#leaderboard) |
| `daily_reset_hour` | `0` | Hour (UTC) when daily points reset |
| `daily_reset_schedule` | — | Per-day reset hour override (e.g. different hour on weekends) |
| `max_pilots` | all | Max pilots in single-table modes |
| `max_pilots_2t` | all | Max pilots per table in dual-table modes. Falls back to `max_pilots` |
| `max_pilots_3t` | `6` | Max pilots per table in triple-table modes. Falls back to `max_pilots_2t` |
| `show_all_pilots` | `false` | `true` = split into multiple fields showing all pilots |
| `show_pilot_card` | `false` | `true` = show career stats card per pilot (requires Foothold v4.5+) |
| `pilot_card_icon` | `🔸` | Emoji shown at the start of the pilot career card line |
| `show_session_card` | `false` | `true` = show session combat stats card per pilot |
| `session_card_icon` | `🔸` | Emoji shown at the start of the session stats card line |
| `show_daily_card` | `false` | `true` = show daily combat stats card per pilot |
| `daily_card_icon` | `🔸` | Emoji shown at the start of the daily stats card line |
| `show_punishment` | `false` | `true` = show punishment badges |
| `excluded_ucids` | none | List of UCIDs to hide from the leaderboard |
| `admin` | `Admin` | Comma-separated Discord role name(s) and/or username(s) allowed to view other players' stats with `/fh_report player` |
| `show_player_cmd_hint` | `true` | `true` = add a footer reminder pointing players to `/fh_report player` |
| `player_cmd_hint_text` | `Type /fh_report player to see your own stats.` | Customize the footer reminder text |
| `disable_updates` | `false` | `true` = this instance never reads, posts, or edits anything for this server — see [Duplicate installs](#duplicate-installs--disable_updates) below |
| `saves_dir` | auto | Override Foothold saves path. Only needed for non-standard locations |

### Example config

```yaml
DEFAULT:
  update_interval: 300
  bar_length: 40
  strip_callsign: true
  points_order: 3DS, BS, BR
  show_pilot_card: true
  show_punishment: true

DCS_Server:                           # instance name from nodes.yaml
  channel_id: 1458145804685541508
  campaign_name: "Operation — FootHold"
  excluded_ucids:
    - e435a8583ad34583b7a709f58d98a6af
```

---

## Zone display

| Zone type | Counted in bar | Listed in column |
|---|---|---|
| Active BLUE / RED | ✅ | ✅ Top, sorted by level and damage |
| Suspended BLUE / RED | ✅ | ✅ Bottom, shown as fully filled |
| Neutral (`side=0`) | ✅ | ❌ |
| Hidden (name starts with `hidden`) | ❌ | ❌ |

Neutral zones are counted in the progress bar but not listed. Suspended zones appear at the bottom shown as fully filled.

---

## Upgrade slot display (`slot_status`)

**`slot_status: false`** (default) — shows zone level as fully filled:
```
Bardufoss    🔹🔹🔹
Kalixfors    🔹🔹🔹🔹
```

**`slot_status: true`** — shows active vs destroyed upgrade slots (first 5 slots only):
```
Bardufoss    🔹◇◇     ← 3 slots, only 1 active
Kalixfors    🔹🔹🔹🔹  ← 4 slots, all active
```

- `🔹` = active BLUE slot · `◇` = destroyed BLUE slot
- `🔺` = active RED slot · `△` = destroyed RED slot

Only the first 5 upgrade slots are shown. Higher-numbered slots serve other purposes and are excluded.

---

## Leaderboard

### Points display (`points_order`)

Comma-separated values cycle through modes on each update: `points_order: 3DS, BS, BR`

Modes with Daily (`D`) are skipped when no daily data exists yet. Session modes are skipped when no session data exists.

| Mode | Tables | Sorted by | Shows |
|---|---|---|---|
| `R` | 1 | Rank | `(R: nnn)` |
| `S` | 1 | Session | `(S: nnn)` |
| `D` | 1 | Daily | `(D: nnn)` |
| `BR` | 1 | Rank | `(R · S · D)` |
| `BS` | 1 | Session | `(S · R · D)` |
| `BD` | 1 | Daily | `(D · R · S)` |
| `BDS` | 1 | Daily | `(D · S · R)` |
| `2R` | 2 | Rank / Session | — |
| `2S` | 2 | Session / Rank | — |
| `2D` | 2 | Daily / Rank | — |
| `2DS` | 2 | Daily / Session | — |
| `3R` | 3 | Rank / Session / Daily | — |
| `3S` | 3 | Session / Rank / Daily | — |
| `3D` | 3 | Daily / Rank / Session | — |
| `3DS` | 3 | Daily / Session / Rank | — |

### Callsign stripping (`strip_callsign: true`)

- `UZI 1-1 | Pilot1` → `Pilot1`
- `GUNSTAR 11 | Pilot2 | 307` → `Pilot2` ← numeric trailing segment discarded
- `Ford 1 - Pilot3` → `Pilot3`
- `[SQD] Pilot4` → `[SQD] Pilot4` ← squadron tags preserved

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
| 120,000 | Captain |
| 155,000 | Major |
| 195,000 | Lieutenant Colonel |
| 240,000 | Colonel |
| 290,000 | Brigadier General |
| 345,000 | Major General |
| 405,000 | Lieutenant General |
| 470,000 | General |
| 540,000 | General of the Air Force |

### Pilot career card (`show_pilot_card: true`)

Requires Foothold v4.5 or later. Shows a career stats line below each pilot in rank-ordered tables only. Data sourced from Foothold_Ranks.lua — historical career totals, not session or daily stats. Values of zero are omitted. If all values are zero the card is not shown.

The icon preceding the card line is configurable via `pilot_card_icon` (default: 🔸).

```
🥇 `Pilot1` — Colonel (R: 241,500)
·　🔸 129h fixed · 13h helo · 47 kills · 23 traps · 12 refuels · 3 deaths
🥈 `Pilot2` — Lieutenant Colonel (R: 198,320)
·　🔸 89h fixed · 31 kills
·　⚖️ `Pilot2` JAG indictment filed (32 p.p.) 🔨🔨🔨
```

Stats shown:
- Fixed-wing flight hours (total minus helicopter)
- Helicopter hours (only if > 0)
- Total kills
- Carrier traps
- In-flight refuels received
- Pilot deaths

### Session and daily stats cards (`show_session_card` / `show_daily_card`)

Similar cards showing combat stats for the current session or the current day, shown only on session-ordered or daily-ordered tables respectively. Data comes from the campaign save file (playerStats), not from career totals.

```
🥇 `Pilot1` — Staff Sergeant (S: 11,357)
·　🔸 5 Msn · 3 Ach · 5 Air · 4 SAM · 27 Ground · 1 Death
```

Shows up to 7 fields, in priority order: missions completed (any stat key containing the word "mission", abbreviated `Msn`), achievements unlocked (`Ach`), air/helo kills, SAM kills, ground/structure/infantry kills, ship kills, pilot rescues (`Resc`), refuels, deaths. Lowest-priority fields are dropped first if there are more than 7 with data. Deaths is always shown if greater than zero. Values of zero are omitted; if everything is zero the card is not shown.

The daily card uses the same daily reset mechanism as daily leaderboard points (see `daily_reset_hour` above). Icons are configurable independently via `session_card_icon` and `daily_card_icon` (default: 🔸 for both).

**Manual reset**: this plugin has no commands. To manually reset the daily counters (points and stats), delete `saves_dir/.fhc/daily_snapshot.json` — the counter always restarts cleanly at 0, never retroactively counting what was already accumulated. A campaign restart (new map, admin reset) is also detected automatically: if both total points and total kills drop for common players, the snapshot resets on its own.

---

## `/fh_report player` — personal stats command

A read-only slash command that shows a single player's full stats as a private (ephemeral) message — no buttons, nothing editable, just information.

```
/fh_report player
/fh_report player player_name:Pilot1
```

- Run it in the channel where FH_Report posts the campaign embed — the server is detected automatically from that channel, no need to specify it.
- **Without `player_name`**: shows your own stats, resolved via your linked Discord account (the same link used by `/linkme`). If your Discord isn't linked yet, you'll be prompted to run `/linkme` first.
- **With `player_name`**: only available to admins (see `admin` config option below). Everyone else gets a permission error and should leave it empty to see their own stats.

The embed shows, in order: UCID, Rank/Session/Daily Points, Last seen, Daily Stats (full detail, only non-zero fields, delta since the last reset), Session Stats (full detail for the current session), Career Stats (from `Foothold_Ranks.lua`), and current mission status.

Unlike the compact cards on the main campaign embed, Daily Stats and Session Stats here show **every individual stat key**, not just the summarized categories — nothing is capped or dropped. Fields are sorted in a fixed order: missions (any key containing "mission") → achievements → air → helo → SAM → infantry → ground units → structure → ship → pilot rescues → refuels → any other/unrecognized stat key → deaths always last.

### Who can look up other players (`admin`)

```yaml
admin: Admin, SomeSpecificUser
```

Comma-separated list — each entry can be a Discord **role name** (as defined in your server) or a specific **username**. Defaults to `Admin` if not set. Anyone not on this list can only ever see their own stats.

### Footer reminder (`show_player_cmd_hint`)

By default, the main campaign embed's footer includes a short reminder pointing players to the command:

```
Type /fh_report player to see your own stats.
```

Disable with `show_player_cmd_hint: false`, or customize the wording with `player_cmd_hint_text`.

---

## Punishment badges (`show_punishment: true`)

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

Requires the DCSServerBot Punishment plugin. If not present, the option is silently ignored.

---

## Duplicate installs (`disable_updates`)

If the same Foothold instance is reachable from more than one `fh_report` installation in the same cluster — for example, running one config next to the master and another on an agent box that hosts that instance — both installations will try to manage the same Discord message.

Starting in this version, that can no longer produce **duplicate messages**: before posting, the plugin checks the target channel for an existing FH_Report message matching that campaign and adopts it instead of creating a new one. So even with two configs pointing at the same channel, you'll only ever see one message.

What it doesn't prevent on its own is **both configs updating that same message** — since each installation runs its own update cycle, the embed would flip between the two configurations (e.g. different `points_order`, `bar_style_emoji`, etc.) every time either one refreshes.

To avoid that, set `disable_updates: true` on every duplicate copy except the one that should actually be in control:

```yaml
DCS_Server:
  channel_id: 1458145804685541508
  campaign_name: "Operation — FootHold"
  disable_updates: true   # this copy stays completely silent for this server
```

When `disable_updates: true`, that instance skips the server entirely on every cycle — no file reads, no posts, no edits — as if it weren't listed in the config at all. Omitting the option (or leaving it `false`) is the normal, default behavior.

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

---

## Changelog

See [Releases](https://github.com/pierpaolobirdi/fh_report-dcsserverbot-plugin/releases) for full release notes.
