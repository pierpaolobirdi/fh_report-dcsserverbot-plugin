# __version__ is read by DCSServerBot's core as the plugin's DATABASE SCHEMA version
# (core/plugin.py: it expects "major.minor" and logs "migrated to version ..." when it
# changes). Fh_Report has no tables, so leave it at "1.0"; change it only together with a
# database change.
__version__ = "1.0"

# The plugin's own release, x.y.z. This is the single source of the version: the footers,
# /fh_report upgrade, install.cmd and external tools (Nova_Updates) all read it from here.
# DCSServerBot does not look at it. Keep the line in this exact shape: install.cmd and the
# upgrade read it with a pattern.
PLUGIN_RELEASE = "14.6.5"
