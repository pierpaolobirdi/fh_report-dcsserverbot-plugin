"""How the plugin talks to a DCSServerBot node, checked against the real API shapes
(DCSServerBot development branch): list_directory returns (folder, full paths, newest
first) and lists nothing for a missing folder; write_file never creates folders;
remove_file takes a glob pattern; remote nodes offer the same calls over the bus."""
import asyncio
import fnmatch
import json
import logging
import os
import posixpath

from conftest import FIXTURES, FileNode, UploadStatus, commands, make_plugin
from test_update_server import _Bot, _Channel


def run(coro):
    return asyncio.run(coro)


# ── list_directory shapes ─────────────────────────────────────────────────────

def test_node_listing_reads_dcssb_tuple_and_older_plain_lists():
    assert commands._node_listing(("/s", ["/s/b.lua", "/s/a.lua"])) == (["b.lua", "a.lua"], True)
    assert commands._node_listing(("C:/s", ["C:/s\\b.lua"])) == (["b.lua"], True)
    assert commands._node_listing(["a.lua", "b.lua"]) == (["a.lua", "b.lua"], False)


def _touch(path, mtime):
    path.write_text("x")
    os.utime(path, (mtime, mtime))


def test_fallback_picks_the_most_recently_modified_save(tmp_path):
    _touch(tmp_path / "foothold_z_old.lua", 1_000)
    _touch(tmp_path / "foothold_a_new.lua", 2_000)                  # newest, but first by name
    _touch(tmp_path / "Foothold_Ranks.lua", 3_000)                  # never a campaign save
    _touch(tmp_path / "notes.lua", 4_000)
    (tmp_path / "foothold.status").write_text(r"C:\Gone\foothold_deleted.lua")   # stale pointer
    assert run(commands.find_persistence_file(str(tmp_path), FileNode())) == str(tmp_path / "foothold_a_new.lua")


def test_fallback_without_a_status_file_and_with_nothing(tmp_path):
    assert run(commands.find_persistence_file(str(tmp_path), FileNode())) is None
    _touch(tmp_path / "foothold_x.lua", 1)
    assert run(commands.find_persistence_file(str(tmp_path), FileNode())) == str(tmp_path / "foothold_x.lua")


class _OldListingNode(FileNode):
    """An older DCSSB: list_directory returns plain names, no extra arguments."""
    async def list_directory(self, path):
        return sorted(os.listdir(path))


def test_fallback_still_works_with_the_older_listing_shape(tmp_path):
    _touch(tmp_path / "foothold_a.lua", 5)
    _touch(tmp_path / "foothold_b.lua", 1)
    assert run(commands.find_persistence_file(str(tmp_path), _OldListingNode())) == str(tmp_path / "foothold_b.lua")


# ── is the folder there? ──────────────────────────────────────────────────────

def test_dir_exists_distinguishes_missing_folders_that_list_as_empty(tmp_path):
    (tmp_path / "Saves [1]").mkdir()                                 # glob characters in the name
    node = FileNode()
    assert run(commands._dir_exists(node, str(tmp_path / "Saves [1]"))) is True
    assert run(commands._dir_exists(node, str(tmp_path / "Saves [1]") + os.sep)) is True
    assert run(commands._dir_exists(node, str(tmp_path / "Missing"))) is False
    assert run(commands._dir_exists(node, str(tmp_path / "x" / "y" / "Saves"))) is False
    (tmp_path / "afile").write_text("x")
    assert run(commands._dir_exists(node, str(tmp_path / "afile"))) is False      # a file is not a folder


class _FailingNode(FileNode):
    async def list_directory(self, path, **kw):
        raise TimeoutError()


def test_dir_exists_says_unknown_when_it_cannot_tell(tmp_path):
    assert run(commands._dir_exists(_FailingNode(), str(tmp_path))) is None
    assert run(commands._dir_exists(_OldListingNode(), str(tmp_path))) is None   # no pattern support


# ── write self-test ───────────────────────────────────────────────────────────

def test_self_test_waits_quietly_for_a_missing_folder_then_runs_and_cleans_up(tmp_path, caplog):
    saves = tmp_path / "Saves"
    commands._write_self_tested.discard(str(saves))
    log = logging.getLogger("fh_report.tests")
    with caplog.at_level(logging.DEBUG):
        run(commands.run_write_self_test(FileNode(), str(saves), log=log))
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert str(saves) not in commands._write_self_tested and not saves.exists()   # retried later
    saves.mkdir()
    run(commands.run_write_self_test(FileNode(), str(saves), log=log))
    assert str(saves) in commands._write_self_tested
    assert os.listdir(saves) == []                                               # test file removed via the node


def test_self_test_runs_when_existence_cannot_be_told(tmp_path):
    saves = tmp_path / "Saves"
    saves.mkdir()
    commands._write_self_tested.discard(str(saves))
    run(commands.run_write_self_test(_FailingNode(), str(saves)))
    assert str(saves) in commands._write_self_tested


# ── .fhc folder and deleting through the node ─────────────────────────────────

def test_fhc_folder_is_created_through_the_node(tmp_path):
    run(commands._ensure_fhc_dir(FileNode(), str(tmp_path)))
    assert (tmp_path / ".fhc").is_dir()


def test_remove_file_gets_an_escaped_pattern(tmp_path):
    folder = tmp_path / "Saves [1]" / ".fhc"                         # '[1]' would be a glob class
    folder.mkdir(parents=True)
    target = folder / "daily_snapshot.json"
    target.write_text("{}")
    run(commands._remove_file(FileNode(), str(target)))
    assert not target.exists()


def test_old_file_cleanup_is_attempted_once_per_run(tmp_path):
    calls = []

    class Spy(FileNode):
        async def remove_file(self, path):
            calls.append(path)
    node = Spy()
    for _ in range(3):
        run(commands._remove_legacy_file(node, str(tmp_path / "daily_snapshot.json")))
    assert len(calls) == 1


# ── a remote agent node, simulated ────────────────────────────────────────────

class RemoteNode:
    """An agent's file system seen through the bus: nothing exists locally, folders must be
    created explicitly, and a write is accepted only if the folder is there."""
    def __init__(self, folders, files=None):
        self.folders, self.files, self.removed = set(folders), dict(files or {}), []

    async def read_file(self, path):
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]

    async def write_file(self, target, source, overwrite=False):
        if posixpath.dirname(target) not in self.folders:
            return UploadStatus.WRITE_ERROR
        with open(source, "rb") as f:
            self.files[target] = f.read()
        return UploadStatus.OK

    async def list_directory(self, path, *, pattern="*", order=None, is_dir=False, ignore=None, traverse=False):
        path = path.rstrip("/")
        if is_dir:
            found = [d for d in self.folders if posixpath.dirname(d) == path and fnmatch.fnmatch(posixpath.basename(d), pattern)]
        else:
            found = [f for f in self.files if posixpath.dirname(f) == path and fnmatch.fnmatch(posixpath.basename(f), pattern)]
        return path, sorted(found)

    async def create_directory(self, path):
        self.folders.add(path)

    async def remove_file(self, path):
        self.removed.append(path)
        for f in [f for f in self.files if fnmatch.fnmatch(f, path)]:
            del self.files[f]


class _RemoteServer:
    status = None

    def __init__(self, node):
        self.node, self.instance = node, type("I", (), {"name": "inst"})()


def _remote(extra_files=None, folders=("/agent/Saves",)):
    files = {"/agent/Saves/Foothold_Ranks.lua": open(os.path.join(FIXTURES, "ranks_new.lua"), "rb").read(),
             "/agent/Saves/foothold_x.lua": open(os.path.join(FIXTURES, "foothold_new.lua"), "rb").read()}
    files.update(extra_files or {})
    return RemoteNode(folders, files)


def _remote_cycle(tmp_path, node, plugin=None):
    plugin = plugin or make_plugin(bot=_Bot(_Channel({})), _message_ids_file=str(tmp_path / "ids.json"))
    run(plugin._update_server(_RemoteServer(node), {"saves_dir": "/agent/Saves", "channel_id": 5,
                                                    "campaign_name": "C", "report_layout": "D"}))
    return plugin


def test_remote_node_gets_its_fhc_folder_and_files_created_there(tmp_path):
    node = _remote()
    _remote_cycle(tmp_path, node)
    assert "/agent/Saves/.fhc" in node.folders
    assert "/agent/Saves/.fhc/fhr_daily_snapshot.json" in node.files
    assert not os.path.exists("/agent")                                          # nothing leaked onto this machine


def test_remote_node_missing_saves_folder_creates_nothing(tmp_path):
    node = RemoteNode(folders=set())
    plugin = make_plugin(bot=_Bot(_Channel({})), _message_ids_file=str(tmp_path / "ids.json"))
    run(plugin._update_server(_RemoteServer(node), {"saves_dir": "/agent/Saves", "channel_id": 5,
                                                    "campaign_name": "C", "report_layout": "D"}))
    assert node.folders == set() and node.files == {}


def test_remote_old_daily_file_is_migrated_then_removed_through_the_node(tmp_path):
    legacy = {"/agent/Saves/.fhc/daily_snapshot.json": json.dumps({"date": "2026-09-01", "snapshot": {"x": 1}}).encode()}
    node = _remote(legacy, folders=("/agent/Saves", "/agent/Saves/.fhc"))
    plugin = _remote_cycle(tmp_path, node)
    assert "/agent/Saves/.fhc/fhr_daily_snapshot.json" in node.files             # written under the new name
    assert "/agent/Saves/.fhc/daily_snapshot.json" in node.files                 # old one kept for now
    _remote_cycle(tmp_path, node, plugin)
    assert "/agent/Saves/.fhc/daily_snapshot.json" not in node.files             # removed on a later read
    assert node.removed == ["/agent/Saves/.fhc/daily_snapshot.json"]
