"""Fails (exit 1) if the exe's embedded file version does not match PLUGIN_RELEASE.

    python tools/ci/check_exe_version.py dist/Fh_Report_Config.exe

Windows only (needs pywin32). The expected value is (x, y, z, 0).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from release_info import read_version, version_tuple  # noqa: E402


def exe_version(path):
    import win32api
    info = win32api.GetFileVersionInfo(path, "\\")
    ms, ls = info["FileVersionMS"], info["FileVersionLS"]
    return (ms >> 16, ms & 0xFFFF, ls >> 16, ls & 0xFFFF)


def main(argv):
    expected = version_tuple(read_version())
    found = exe_version(argv[1])
    print(f"{argv[1]}: file version {found}, expected {expected}")
    return 0 if found == expected else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
