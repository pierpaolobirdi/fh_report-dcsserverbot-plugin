"""tools/fh_report_config.pyw: the window of the configuration editor (needs tkinter and a display; skipped without)."""
import importlib.machinery
import importlib.util
import os
import shutil
import sys

import pytest

pytest.importorskip("ruamel.yaml")
tk = pytest.importorskip("tkinter")

from conftest import ROOT  # noqa: E402

EXAMPLE = os.path.join(ROOT, "config", "plugins", "fh_report.yaml")


def _load_window():
    path = os.path.join(ROOT, "tools", "fh_report_config.pyw")
    loader = importlib.machinery.SourceFileLoader("fh_report_config_window", path)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    loader.exec_module(module)
    return module


@pytest.fixture
def window(tmp_path, monkeypatch):
    mod = _load_window()
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("no display for tkinter")
    root.withdraw()
    shown = []
    for name in ("showinfo", "showerror"):
        monkeypatch.setattr(mod.messagebox, name, lambda *a, _n=name, **k: shown.append((_n, a)))
    monkeypatch.setattr(mod.messagebox, "askyesno", lambda *a, **k: True)
    monkeypatch.setattr(mod, "STATE_FILE", str(tmp_path / "state.json"))
    cfg = tmp_path / "install" / "config" / "plugins"
    cfg.mkdir(parents=True)
    shutil.copy(EXAMPLE, cfg / "fh_report.yaml")
    app = mod.App(root, str(tmp_path / "install"))
    app.shown, app.file = shown, cfg / "fh_report.yaml"
    yield app
    root.destroy()


def test_it_opens_the_example_with_every_section_and_no_problem(window):
    assert window.target_box.cget("values") == ("DEFAULT (every server)", "DCS_Server")
    assert [window.notebook.tab(t, "text") for t in window.notebook.tabs()][:3] == ["Server", "Layout", "Refresh and reset"]
    assert len(window.notebook.tabs()) == len(window.model.sections)
    assert window.problems.get(0) == "No problems found."
    assert "channel_id" not in {n for page in window.pages.values() for n in page.rows}      # server-only: not under DEFAULT


def test_a_typed_value_is_stored_and_a_wrong_one_is_refused_with_the_reason(window):
    row = window.row("update_interval")
    row.var.set("90")
    assert window.config.get(window.target, "update_interval")[0] == 90 and window.dirty
    assert row.now.cget("text").startswith("Now: 90") and "set in this block" in row.now.cget("text")
    row.var.set("fast")
    assert row.error.cget("text") == "⚠ must be a whole number"
    assert window.config.get(window.target, "update_interval")[0] == 90          # the bad text did not replace it
    row.var.set("")
    assert window.config.get(window.target, "update_interval")[1] == "default"   # an empty field takes the line out


def test_every_kind_of_field_changes_the_file(window):
    window.row("show_map").var.set("false")
    window.row("report_layout").var.set("DP, SR")
    window.row("excluded_ucids").text.insert("1.0", "abc\ndef")
    window.row("excluded_ucids").changed()
    window.row("daily_reset_schedule").days["sat"].set("7:00")
    text = window.config.dumps()
    assert "show_map: false" in text and "report_layout: DP, SR" in text
    assert "    - abc\n    - def" in text and "    sat: 7:00" in text
    window.row("report_layout").var.set("XYZ")
    assert window.row("report_layout").error.cget("text").startswith("⚠ is not valid")


def test_a_server_block_shows_what_it_inherits_and_server_only_options(window):
    window.target_var.set("DCS_Server")
    window._pick_target()
    assert "channel_id" in window.row("channel_id").spec.name
    assert window.row("channel_id").var.get() == "1234567890123456789"
    assert "from DEFAULT" in window.row("max_zones").now.cget("text")
    window.row("show_new_release").var.set("false")                         # only read from DEFAULT: the field is off
    assert window.row("show_new_release").disabled
    assert window.config.get(window.target, "show_new_release")[1] != "own"


def test_saving_writes_the_file_keeps_a_backup_and_says_to_restart(window):
    window.row("bar_length").var.set("55")
    assert window.save() is True
    assert "  bar_length: 55" in window.file.read_text(encoding="utf-8")
    assert any(f.endswith(".bak") for f in os.listdir(window.file.parent))
    assert window.shown[-1][0] == "showinfo" and "restart" in window.shown[-1][1][1]
    assert not window.dirty


def test_a_broken_file_is_explained_and_does_not_open(window, tmp_path):
    bad = tmp_path / "bad" / "config" / "plugins"
    bad.mkdir(parents=True)
    (bad / "fh_report.yaml").write_text("DEFAULT:\n  show_map: true\n  show_map: false\n", encoding="utf-8")
    assert window.open(str(tmp_path / "bad")) == "broken"
    assert window.shown[-1][0] == "showerror" and "line 3" in window.shown[-1][1][1]
    assert window.open(str(tmp_path)) == "nofile"


def test_the_window_is_dressed_in_the_dark_theme_and_shows_booleans_in_yaml_style(window):
    theme = sys.modules["fh_report_config_theme"]
    assert window.root.cget("background") == theme.BG
    assert window.problems.cget("background") == theme.FIELD
    assert window.row("enable_updates").now.cget("text").startswith("Now: true")      # not Python's True


def test_the_check_option_loads_the_option_list_without_a_window_and_reports_the_release(tmp_path):
    mod = _load_window()
    assert mod.check() == 0
    report = tmp_path / "report.txt"
    assert mod.check(str(report)) == 0
    first = report.read_text(encoding="utf-8").splitlines()[0]
    assert first == f"OK Ver. {sys.modules['fh_report_config_core'].release()}"
    assert first.endswith(open(os.path.join(ROOT, "plugins", "fh_report", "version.py")).read().split('PLUGIN_RELEASE = "')[1].split('"')[0])


def test_the_icon_is_a_real_multi_size_ico_and_a_computer_that_cannot_use_it_is_not_bothered(window):
    ico = os.path.join(ROOT, "tools", "fh_report_config.ico")
    data = open(ico, "rb").read()
    assert data[:4] == b"\x00\x00\x01\x00" and int.from_bytes(data[4:6], "little") >= 5      # ICO header, several sizes
    assert os.path.getsize(ico) < 200_000
    sys.modules["fh_report_config_window"].set_icon(window.root)                              # no exception (Linux refuses it)
