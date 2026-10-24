"""Release info for the CI build of the configuration editor (also handy locally).

Prints the version, writes build/release_notes.md (the latest changes to the editor) and
build/version_info.txt (the Windows file-version resource of the .exe) and, in GitHub Actions,
exports `version` through $GITHUB_OUTPUT.

    python tools/ci/release_info.py
"""
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
VERSION_FILE = os.path.join("plugins", "fh_report", "version.py")
NOTE_PATHS = ("tools", os.path.join("plugins", "fh_report", "schemas"))


def read_version(root=ROOT):
    """PLUGIN_RELEASE of version.py (the single source of the release number)."""
    with open(os.path.join(root, VERSION_FILE), encoding="utf-8") as f:
        return re.search(r'^PLUGIN_RELEASE\s*=\s*"([^"]+)"', f.read(), re.M).group(1)


def version_tuple(version):
    """(14, 6, 10, 0) from '14.6.10'."""
    x, y, z = (int(n) for n in version.split("."))
    return (x, y, z, 0)


def version_info(version):
    """The text PyInstaller's --version-file reads: what Windows shows in the .exe's Details tab."""
    nums = version_tuple(version)
    return f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={nums}, prodvers={nums}, mask=0x3f, flags=0x0, OS=0x40004,
                    fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('FileDescription', 'Fh_Report configuration editor'),
      StringStruct('FileVersion', '{version}'),
      StringStruct('InternalName', 'Fh_Report_Config'),
      StringStruct('OriginalFilename', 'Fh_Report_Config.exe'),
      StringStruct('ProductName', 'Fh_Report'),
      StringStruct('ProductVersion', '{version}')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""


def change_notes(version, root=ROOT, count=8):
    """Ver. x.y.z plus the subjects of the last commits that touched the editor or the schema."""
    try:
        log = subprocess.run(["git", "log", f"-{count}", "--format=- %s", "--", *NOTE_PATHS], cwd=root,
                             capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        log = ""
    return f"Ver. {version}\n\nLatest changes to the editor:\n\n{log}\n" if log else f"Ver. {version}\n"


def main(root=ROOT):
    version = read_version(root)
    out_dir = os.path.join(root, "build")
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "release_notes.md"), "w", encoding="utf-8") as f:
        f.write(change_notes(version, root))
    with open(os.path.join(out_dir, "version_info.txt"), "w", encoding="utf-8") as f:
        f.write(version_info(version))
    print(f"version={version}")
    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a", encoding="utf-8") as f:
            f.write(f"version={version}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
