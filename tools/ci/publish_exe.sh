#!/usr/bin/env bash
# Publishes the freshly built exe to the `builds` branch of the repository.
#
# The branch always holds exactly ONE commit (it is recreated and force-pushed on every build), so
# binaries never pile up in the repository history. `dev`, `main` and the Releases are not touched.
#
# Usage: publish_exe.sh <remote-url> <version> <commit-sha> <notes-file> <exe-dir>
#   <exe-dir> must contain Fh_Report_Config.exe and Fh_Report_Config.exe.sha256
set -euo pipefail

remote=$1 version=$2 sha=$3 notes=$4 exe_dir=$5
work=$(mktemp -d)

cp "$exe_dir/Fh_Report_Config.exe" "$exe_dir/Fh_Report_Config.exe.sha256" "$work/"
cp "$notes" "$work/CHANGES.md"
cat > "$work/README.md" <<README
# Latest build of the Fh_Report configuration editor: Ver. $version

Click **Fh_Report_Config.exe** above, then the download button (or "Download raw file").
Keep it next to \`fh_report_config.cmd\` (the \`tools\` folder of the release) or run it from anywhere:
it asks for the DCSServerBot folder the first time.

- Built from commit \`$sha\` of the \`dev\` branch
- Built on $(date -u '+%Y-%m-%d %H:%M UTC') by GitHub Actions
- SHA-256: \`$(cut -d' ' -f1 "$work/Fh_Report_Config.exe.sha256")\`

This branch is rewritten on every build and only ever contains the latest exe.
What changed: [CHANGES.md](CHANGES.md).
README

cd "$work"
git init -q -b builds
git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
git add -A
git commit -q -m "Ver. $version (build of $sha)"
git push -q --force "$remote" builds
echo "Published Ver. $version to the builds branch"
