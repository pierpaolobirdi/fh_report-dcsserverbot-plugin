"""tools/ci: the version of the build, the file-version resource of the .exe and the publication to the `builds` branch."""
import importlib.util
import os
import shutil
import subprocess

import pytest

from conftest import ROOT


def _load(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, "tools", "ci", f"{name}.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


info = _load("release_info")


def test_the_version_comes_from_version_py_and_becomes_the_exe_file_version():
    version = info.read_version()
    assert version == open(os.path.join(ROOT, "plugins", "fh_report", "version.py")).read().split('PLUGIN_RELEASE = "')[1].split('"')[0]
    assert info.version_tuple("14.6.10") == (14, 6, 10, 0)
    text = info.version_info("14.6.10")
    assert "filevers=(14, 6, 10, 0)" in text and "StringStruct('FileVersion', '14.6.10')" in text
    assert "Fh_Report_Config.exe" in text
    compile(text.replace("VSVersionInfo(", "dict(").replace("FixedFileInfo(", "dict(").replace("StringFileInfo(", "list(").replace(
        "StringTable(", "tuple(").replace("StringStruct(", "tuple(").replace("VarFileInfo(", "list(").replace("VarStruct(", "tuple("), "v", "eval")


def test_main_writes_the_notes_and_the_version_resource_and_exports_the_version(tmp_path, monkeypatch):
    (tmp_path / "plugins" / "fh_report").mkdir(parents=True)
    (tmp_path / "plugins" / "fh_report" / "version.py").write_text('__version__ = "1.0"\nPLUGIN_RELEASE = "9.8.7"\n')
    out = tmp_path / "gh_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    assert info.main(str(tmp_path)) == 0
    assert out.read_text() == "version=9.8.7\n"
    assert (tmp_path / "build" / "release_notes.md").read_text().startswith("Ver. 9.8.7")
    assert "filevers=(9, 8, 7, 0)" in (tmp_path / "build" / "version_info.txt").read_text()


@pytest.mark.skipif(shutil.which("bash") is None or shutil.which("git") is None, reason="needs bash and git")
def test_publishing_leaves_one_commit_on_the_builds_branch_with_the_exe_and_its_notes(tmp_path):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    exe_dir = tmp_path / "dist"
    exe_dir.mkdir()
    (exe_dir / "Fh_Report_Config.exe").write_bytes(b"MZ-fake")
    (exe_dir / "Fh_Report_Config.exe.sha256").write_text("abc123  Fh_Report_Config.exe\n")
    notes = tmp_path / "notes.md"
    notes.write_text("Ver. 1.2.3\n")
    script = os.path.join(ROOT, "tools", "ci", "publish_exe.sh")
    for version in ("1.2.3", "1.2.4"):                                 # twice: the branch stays a single commit
        subprocess.run(["bash", script, str(remote), version, "deadbeef", str(notes), str(exe_dir)], check=True,
                       capture_output=True)
    git = lambda *a: subprocess.run(["git", "--git-dir", str(remote), *a], capture_output=True, text=True, check=True).stdout
    assert git("rev-list", "--count", "builds").strip() == "1"
    assert sorted(git("ls-tree", "--name-only", "builds").split()) == [
        "CHANGES.md", "Fh_Report_Config.exe", "Fh_Report_Config.exe.sha256", "README.md"]
    readme = git("show", "builds:README.md")
    assert "Ver. 1.2.4" in readme and "deadbeef" in readme and "abc123" in readme
    assert git("log", "-1", "--format=%s", "builds").strip() == "Ver. 1.2.4 (build of deadbeef)"
