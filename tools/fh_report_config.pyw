"""Fh_Report configuration editor.

A window to read and change config/plugins/fh_report.yaml with every option explained, grouped in
sections, per block (DEFAULT or one server). The file keeps its comments and order, is checked before it
is saved and a copy of the old one is kept. All the work is in fh_report_config_core.py.

Run it with the Python of DCSServerBot (fh_report_config.cmd does):
    pythonw fh_report_config.pyw [path to DCSServerBot, or to fh_report.yaml]
    fh_report_config.pyw --check [report file]   (no window: exit code 0 when its option list loads)
"""
import json
import os
import sys
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fh_report_config_core as core  # noqa: E402
import fh_report_config_theme as theme  # noqa: E402

TITLE = "Fh_Report configuration"
STATE_FILE = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "Fh_Report", "config_editor.json")
NOT_SET = "(not set)"


def _remembered() -> str | None:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f).get("path")
    except (OSError, ValueError):
        return None


def _remember(path: str) -> None:
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({"path": path}, f)
    except OSError:
        pass


class Row:
    """One option: its name, a field to change it, what applies now, and its explanation."""

    def __init__(self, app: "App", parent: ttk.Frame, spec: core.OptionSpec):
        self.app, self.spec = app, spec
        self.disabled = spec.default_only and not app.target.is_default
        self.frame = ttk.Frame(parent, padding=(0, 6))
        self.frame.columnconfigure(1, weight=1)
        title = ttk.Frame(self.frame)
        title.grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(title, text=spec.name, style="Name.TLabel").pack(side="left")
        if spec.required:
            ttk.Label(title, text="  required", style="Bad.TLabel").pack(side="left")
        ttk.Label(title, text=f"  since {spec.since}", style="Muted.TLabel").pack(side="left")
        ttk.Label(self.frame, text=spec.desc, style="Muted.TLabel", wraplength=720, justify="left").grid(
            row=1, column=0, columnspan=3, sticky="w")
        self.var = tk.StringVar()
        self.field = self._make_field()
        self.field.grid(row=2, column=0, columnspan=2, sticky="we", pady=(3, 0))
        self.help_button = ttk.Button(self.frame, text="?", width=3, style="Help.TButton", command=self._toggle_help)
        self.help_button.grid(row=2, column=2, padx=(6, 0), pady=(3, 0))
        self.now = ttk.Label(self.frame, style="Accent.TLabel")
        self.now.grid(row=3, column=0, columnspan=3, sticky="w")
        self.error = ttk.Label(self.frame, style="Bad.TLabel", wraplength=720, justify="left")
        self.error.grid(row=4, column=0, columnspan=3, sticky="w")
        self.help = ttk.Label(self.frame, text=spec.help, wraplength=700, justify="left", style="Help.TLabel")
        self._help_shown = False
        ttk.Separator(self.frame).grid(row=6, column=0, columnspan=3, sticky="we", pady=(6, 0))
        self.load()

    # field ------------------------------------------------------------------------------------

    def _make_field(self):
        spec = self.spec
        if spec.kind == "bool":
            box = ttk.Combobox(self.frame, textvariable=self.var, values=[NOT_SET, "true", "false"],
                               state="disabled" if self.disabled else "readonly", width=14)
            self.var.trace_add("write", lambda *_a: self.changed())
            return box
        if spec.kind == "list":
            self.text = tk.Text(self.frame, height=4, width=60, wrap="none", **theme.text_options())
            self.text.bind("<KeyRelease>", lambda _e: self.changed())
            return self.text
        if spec.kind == "weekdays":
            frame = ttk.Frame(self.frame)
            self.days = {}
            for i, day in enumerate(spec.weekdays):
                ttk.Label(frame, text=day).grid(row=0, column=i)
                var = tk.StringVar()
                var.trace_add("write", lambda *_a: self.changed())
                ttk.Entry(frame, textvariable=var, width=7).grid(row=1, column=i, padx=2)
                self.days[day] = var
            return frame
        entry = ttk.Entry(self.frame, textvariable=self.var, width=70 if spec.name != "report_layout" else 24,
                          state="disabled" if self.disabled else "normal")
        self.var.trace_add("write", lambda *_a: self.changed())
        return entry

    def _typed(self):
        spec = self.spec
        if spec.kind == "bool":
            current = self.var.get()
            return core.UNSET if current in ("", NOT_SET) else current == "true"
        if spec.kind == "list":
            return self.text.get("1.0", "end")
        if spec.kind == "weekdays":
            return {day: var.get() for day, var in self.days.items()}
        return self.var.get()

    def _put(self, shown) -> None:
        spec = self.spec
        self._loading = True
        try:
            if spec.kind == "bool":
                self.var.set(NOT_SET if shown == "" else ("true" if shown else "false"))
            elif spec.kind == "list":
                self.text.delete("1.0", "end")
                self.text.insert("1.0", shown)
            elif spec.kind == "weekdays":
                for day, var in self.days.items():
                    var.set(shown.get(day, ""))
            else:
                self.var.set(shown)
        finally:
            self._loading = False

    # behaviour ----------------------------------------------------------------------------------

    def load(self) -> None:
        cf, target = self.app.config, self.app.target
        own = cf.block(target)
        value = own.get(self.spec.name) if own is not None and self.spec.name in own else None
        self._put(core.value_to_input(self.spec, value) if value is not None or self.spec.kind != "bool" else "")
        self.refresh_now()

    def refresh_now(self) -> None:
        value, origin = self.app.config.get(self.app.target, self.spec.name)
        shown = core.value_to_input(self.spec, value)
        if isinstance(shown, bool):
            shown = "true" if shown else "false"
        if isinstance(shown, dict):
            shown = ", ".join(f"{d} {t}" for d, t in shown.items())
        shown = str(shown).replace("\n", ", ") or "nothing"
        where = {"own": "set in this block", "DEFAULT": "from DEFAULT", "default": "the plugin's own default"}[origin]
        note = "  (only read from DEFAULT)" if self.disabled else ""
        self.now.configure(text=f"Now: {shown}  - {where}{note}")

    def changed(self) -> None:
        if getattr(self, "_loading", False) or self.disabled:
            return
        value, problem = core.parse_input(self.spec, self._typed())
        self.error.configure(text=("⚠ " + problem) if problem else "")
        if problem:
            return
        self.app.config.set(self.app.target, self.spec.name, value)
        self.refresh_now()
        self.app.touched()

    def _toggle_help(self) -> None:
        self._help_shown = not self._help_shown
        if self._help_shown:
            self.help.grid(row=5, column=0, columnspan=3, sticky="we", pady=(4, 0))
        else:
            self.help.grid_remove()


class Page(ttk.Frame):
    """A scrollable page with the options of one section."""

    def __init__(self, parent, app: "App", section: dict):
        super().__init__(parent)
        canvas = tk.Canvas(self, **theme.canvas_options())
        bar = ttk.Scrollbar(self, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas, padding=(12, 8))
        inner.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=bar.set)
        canvas.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")
        canvas.bind("<Enter>", lambda _e: canvas.bind_all("<MouseWheel>", lambda ev: canvas.yview_scroll(-int(ev.delta / 120), "units")))
        canvas.bind("<Leave>", lambda _e: canvas.unbind_all("<MouseWheel>"))
        ttk.Label(inner, text=section["intro"], wraplength=720, justify="left", style="Note.TLabel").pack(
            anchor="w", pady=(0, 4))
        self.rows: dict[str, Row] = {}
        for spec in app.model.in_section(section["id"]):
            if spec.server_only and app.target.is_default:
                continue
            row = Row(app, inner, spec)
            row.frame.pack(fill="x", anchor="w")
            self.rows[spec.name] = row


class App:
    def __init__(self, root: tk.Tk, start: str | None = None):
        self.root = root
        self.model = None
        self.config: core.ConfigFile | None = None
        self.install: core.Install | None = None
        self.target = core.Target(("DEFAULT",))
        self.dirty = False
        self.pages: dict[str, Page] = {}
        root.title(TITLE)
        root.geometry("960x760")
        root.minsize(760, 560)
        theme.apply(root)
        root.protocol("WM_DELETE_WINDOW", self.quit)
        self._menu()
        top = ttk.Frame(root, padding=(16, 12, 16, 8))
        top.pack(fill="x")
        heading = ttk.Frame(top)
        heading.pack(fill="x")
        ttk.Label(heading, text="Fh_Report", style="Title.TLabel").pack(side="left")
        ttk.Label(heading, text="  configuration", style="Muted.TLabel").pack(side="left", pady=(7, 0))
        ttk.Label(heading, text=f"editor Ver. {core.release()}", style="Muted.TLabel").pack(side="right", pady=(7, 0))
        self.where = ttk.Label(top, style="Muted.TLabel")
        self.where.pack(anchor="w", pady=(2, 0))
        pick = ttk.Frame(top)
        pick.pack(fill="x", pady=(10, 0))
        ttk.Label(pick, text="Editing:").pack(side="left")
        self.target_var = tk.StringVar()
        self.target_box = ttk.Combobox(pick, textvariable=self.target_var, state="readonly", width=44)
        self.target_box.pack(side="left", padx=6)
        self.target_box.bind("<<ComboboxSelected>>", lambda _e: self._pick_target())
        ttk.Button(pick, text="Add server...", command=self.add_server).pack(side="left")
        ttk.Button(pick, text="Save", style="Accent.TButton", command=self.save).pack(side="right")
        self.notebook = ttk.Notebook(root)
        self.notebook.pack(fill="both", expand=True, padx=16)
        bottom = ttk.Frame(root, padding=(16, 8))
        bottom.pack(fill="x")
        ttk.Label(bottom, text="Problems", style="Muted.TLabel").pack(anchor="w", pady=(0, 3))
        self.problems = tk.Listbox(bottom, height=5, activestyle="none", **theme.listbox_options())
        self.problems.pack(fill="x")
        self.problems.bind("<Double-Button-1>", lambda _e: self._jump())
        self._problem_list: list[core.Problem] = []
        self.status = ttk.Label(root, anchor="w", padding=(16, 4, 16, 8), style="Muted.TLabel")
        self.status.pack(fill="x")
        root.bind("<Control-s>", lambda _e: self.save())
        if self.open(start or _remembered()) == "nofile":
            self.root.after(50, self.ask_folder)

    # menus and files ----------------------------------------------------------------------------

    def _menu(self) -> None:
        menu = tk.Menu(self.root)
        file = tk.Menu(menu, tearoff=False)
        file.add_command(label="Open DCSServerBot folder...", command=self.ask_folder)
        file.add_command(label="Reload from disk", command=self.reload)
        file.add_command(label="Open the file in a text editor", command=self.open_in_editor)
        file.add_separator()
        file.add_command(label="Save    Ctrl+S", command=self.save)
        file.add_command(label="Quit", command=self.quit)
        menu.add_cascade(label="File", menu=file)
        self.root.configure(menu=menu)

    def ask_folder(self) -> None:
        folder = filedialog.askdirectory(title="DCSServerBot folder (the one with the config folder)")
        if folder and self.open(folder) == "nofile":
            self.root.after(50, self.ask_folder)

    def open(self, path: str | None) -> str:
        """"ok", "nofile" (nothing found there: ask for the folder) or "broken" (found, but it cannot be read:
        the person has been told why and can fix the file from File > Open the file in a text editor)."""
        if not path:
            return "nofile"
        try:
            install = core.find_installation(path)
        except core.ConfigError as e:
            messagebox.showerror(TITLE, str(e))
            return "nofile"
        self.install = install
        try:
            model = core.Model(install.schema_file)
            config = core.ConfigFile.open(install.config_file, model)
        except (core.ConfigError, OSError) as e:
            self.where.configure(text=install.config_file)
            messagebox.showerror(TITLE, f"{install.config_file}\n\n{e}")
            self.set_status("The file cannot be read. Fix it (File > Open the file in a text editor) and use File > Reload.")
            return "broken"
        self.model, self.config = model, config
        _remember(install.root)
        self.dirty = False
        self.target = core.Target(("DEFAULT",))
        self._fill_targets()
        self.rebuild()
        self.where.configure(text=install.config_file)
        self.set_status("Opened.")
        return "ok"

    def reload(self) -> None:
        if self.install and (not self.dirty or messagebox.askyesno(TITLE, "Discard the changes not saved yet?")):
            self.open(self.install.config_file)

    def open_in_editor(self) -> None:
        if self.install and hasattr(os, "startfile"):
            os.startfile(self.install.config_file)          # Windows: the program that opens .yaml files
        elif self.install:
            messagebox.showinfo(TITLE, self.install.config_file)

    # targets and pages ----------------------------------------------------------------------------

    def _fill_targets(self) -> None:
        self._targets = {t.label: t for t in self.config.targets()}
        if "DEFAULT (every server)" not in self._targets:
            default = core.Target(("DEFAULT",))
            self._targets = {default.label: default, **self._targets}
        self.target_box.configure(values=list(self._targets))
        self.target_var.set(self.target.label)

    def _pick_target(self) -> None:
        self.target = self._targets[self.target_var.get()]
        self.rebuild()

    def rebuild(self) -> None:
        selected = self.notebook.index("current") if self.notebook.tabs() else 0
        for tab in self.notebook.tabs():
            self.notebook.forget(tab)
        self.pages = {}
        for section in self.model.sections:
            page = Page(self.notebook, self, section)
            self.notebook.add(page, text=section["title"])
            self.pages[section["id"]] = page
        self.notebook.select(min(selected, len(self.notebook.tabs()) - 1))
        self.show_problems()

    def row(self, name: str) -> Row:
        """The field of an option on the page now shown (used by the tests)."""
        return next(page.rows[name] for page in self.pages.values() if name in page.rows)

    def add_server(self) -> None:
        if self.config is None:
            return
        dialog = tk.Toplevel(self.root)
        dialog.title("Add server")
        dialog.transient(self.root)
        dialog.grab_set()
        fields = {}
        for i, (label, key) in enumerate((("Instance name (as in DCSServerBot, e.g. DCS.release_server)", "name"),
                                           ("Node name (only if the cluster repeats instance names)", "node"),
                                           ("Discord channel ID", "channel"), ("Campaign name", "campaign"))):
            ttk.Label(dialog, text=label).grid(row=i, column=0, sticky="w", padx=8, pady=3)
            fields[key] = tk.StringVar()
            ttk.Entry(dialog, textvariable=fields[key], width=34).grid(row=i, column=1, padx=8, pady=3)

        def create():
            try:
                channel = int(fields["channel"].get().strip())
                target = self.config.add_server(fields["name"].get(), channel, fields["campaign"].get().strip(),
                                                fields["node"].get().strip() or None)
            except ValueError:
                messagebox.showerror(TITLE, "The channel ID is a whole number.", parent=dialog)
                return
            except core.ConfigError as e:
                messagebox.showerror(TITLE, str(e), parent=dialog)
                return
            dialog.destroy()
            self.target = target
            self._fill_targets()
            self.rebuild()
            self.touched()
        ttk.Button(dialog, text="Add", command=create).grid(row=4, column=1, sticky="e", padx=8, pady=8)

    # problems, saving ---------------------------------------------------------------------------------

    def touched(self) -> None:
        self.dirty = True
        self.show_problems()
        self.set_status("Changed - not saved yet.")

    def show_problems(self) -> None:
        self._problem_list = self.config.validate()
        self.problems.delete(0, "end")
        for p in self._problem_list:
            self.problems.insert("end", ("⚠ " if p.level == "warning" else "✖ ") + str(p))
        if not self._problem_list:
            self.problems.insert("end", "No problems found.")

    def _jump(self) -> None:
        picked = self.problems.curselection()
        if not picked or picked[0] >= len(self._problem_list):
            return
        problem = self._problem_list[picked[0]]
        for label, target in self._targets.items():
            if label == problem.where:
                self.target = target
                self.target_var.set(label)
                self.rebuild()
                spec = self.model.options.get(problem.option or "")
                if spec:
                    ids = [s["id"] for s in self.model.sections]
                    self.notebook.select(ids.index(spec.section))

    def set_status(self, text: str) -> None:
        self.status.configure(text=text)

    def save(self) -> bool:
        if self.config is None:
            return False
        errors = [p for p in self.config.validate() if p.level == "error"]
        if errors and not messagebox.askyesno(
                TITLE, f"{len(errors)} problem(s) would be saved:\n\n" + "\n".join(str(p) for p in errors[:6])
                + "\n\nSave anyway?", icon="warning", default="no"):
            return False
        try:
            backup = self.config.save()
        except (core.ConfigError, OSError) as e:
            messagebox.showerror(TITLE, f"Could not save:\n{e}")
            return False
        self.dirty = False
        self.set_status("Saved." + (f" Previous file kept as {os.path.basename(backup)}." if backup else ""))
        messagebox.showinfo(TITLE, "Saved.\n\nDCSServerBot reads this file when it starts: restart the bot "
                                   "for the changes to apply.")
        return True

    def quit(self) -> None:
        if self.dirty and self.config is not None:
            answer = messagebox.askyesnocancel(TITLE, "Save the changes before closing?")
            if answer is None or (answer and not self.save()):
                return
        self.root.destroy()


def check(report: str | None = None) -> int:
    """--check [report file]: load the option catalog and the schema without opening a window. The first line
    of the report is `OK Ver. x.y.z` (the build runs this on the packed program) or `FAILED ...`."""
    try:
        model = core.Model()
        ok = bool(model.options and model.sections)
        text = (f"OK Ver. {core.release()}" if ok else "FAILED: no options") + \
            f"\nPython {sys.version.split()[0]}, Tk {tk.TkVersion}"
    except Exception as e:  # noqa: BLE001 - whatever it is, the build must see it
        ok, text = False, f"FAILED: {e!r}"
    if report:
        with open(report, "w", encoding="utf-8") as f:
            f.write(text + "\n")
    return 0 if ok else 1


def main() -> None:
    if "--check" in sys.argv:
        i = sys.argv.index("--check")
        sys.exit(check(sys.argv[i + 1] if len(sys.argv) > i + 1 else None))
    root = tk.Tk()
    App(root, sys.argv[1] if len(sys.argv) > 1 else None)
    root.mainloop()


if __name__ == "__main__":
    main()
