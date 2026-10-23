# Fh_Report plugin — notes for Claude

## Version layout (do not break)

- `PLUGIN_RELEASE` in `plugins/fh_report/version.py` is the single source of the release version.
  `__version__` in the same file is DCSServerBot's database-schema version: leave it at `"1.0"`
  (the core expects `major.minor` there and logs `migrated to version ...` when it changes).
- `commands.py` keeps the **bridge line** `FH_REPORT_RELEASE = "x.y.z"`, equal to `PLUGIN_RELEASE`
  (a test checks it). Installations older than 14.6.1 validate a downloaded release by
  reading that exact line, so every release must carry it or `/fh_report upgrade` refuses to
  jump straight to it. Bump it together with `PLUGIN_RELEASE`; never rename it.
- The older upgrade also copies only the files it knows. Do not add files under `plugins/fh_report/`
  without reading the compatibility tests at the end of `tests/test_release_version.py`.

## Describing an option (do not break)

- Every option of `fh_report.yaml` is described once, in `tools/fh_report_options.yaml` (section, one
  line `desc`, default, release it appeared in, and its `help`: the exact comment text of the header). Change
  it there and run `python tools/sync_option_docs.py`: it writes the `desc` lines of the schema, the copy of
  the schema inside `migrate_config.py`, `HEADER_COMMENT` in `migrate_config.py` and the header of
  `config/plugins/fh_report.yaml`. Do not edit those by hand. A test fails if they are out of date.
- The README option table is written by hand (grouped rows, links); a test only checks that every option is in it.
- pykwalify refuses unknown keys in a schema rule (only `desc` and `example` are allowed), so sections and
  release numbers live in the catalog only. Add a new option to the catalog, the schema, `migrate_config.py`
  and the README together; the tests say which one is missing.

## Pending: retire the bridge line at the next major release (15.x.x)

Decision of the owner (do it only when the owner publishes 15.x.x; ask first):

1. Remove the `FH_REPORT_RELEASE` line and its comment from `commands.py`.
2. Remove from `tests/test_release_version.py` the bridge tests (`test_bridge_*`,
   `test_the_bridge_line_is_not_used_by_the_code`) and the compatibility tests at the end of the
   file (`OLD_UPGRADE_FILES`, `DELIVERED_BY_MIGRATE_CONFIG`).
3. Update the "Versioning" part of `README.md` and the comment above the line.
4. Say it in the release notes: anyone coming from a version older than the **first published
   release that carries the bridge line** (the first release published after 14.6.0) must
   update with `install.cmd`, not with `/fh_report upgrade`, which refuses that jump.
