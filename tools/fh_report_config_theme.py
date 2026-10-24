"""Colours and ttk styles of the Fh_Report configuration editor: a dark window with an amber accent."""
from tkinter import font as tkfont, ttk

BG = "#12161d"          # window
PANEL = "#1b212b"       # tabs, help text, buttons
PANEL_2 = "#262e3b"     # the selected tab, hovered controls
FIELD = "#0d1016"       # where you type
BORDER = "#303a49"
TEXT = "#e8ebf0"
MUTED = "#8d98aa"
ACCENT = "#f0a43a"      # amber
ACCENT_HOVER = "#ffbb5c"
ACCENT_TEXT = "#1a1204"
BAD = "#ff6b6b"
WARN = "#f2c14e"
GOOD = "#5cc99a"

PREFERRED_FONTS = ("Segoe UI", "Helvetica Neue", "Noto Sans", "DejaVu Sans", "Arial")
FONT = ("Segoe UI", 10)           # replaced in apply() by what the computer has
FONT_BOLD = ("Segoe UI", 10, "bold")
FONT_TITLE = ("Segoe UI", 18, "bold")
FONT_NOTE = ("Segoe UI", 10, "italic")


def _choose_fonts(root) -> None:
    """Segoe UI on Windows; on a computer without it, the first of PREFERRED_FONTS it has."""
    global FONT, FONT_BOLD, FONT_TITLE, FONT_NOTE
    available = set(tkfont.families(root))
    family = next((f for f in PREFERRED_FONTS if f in available), None)
    if family is None:
        family = tkfont.nametofont("TkDefaultFont").actual("family")
    FONT, FONT_BOLD = (family, 10), (family, 10, "bold")
    FONT_TITLE, FONT_NOTE = (family, 18, "bold"), (family, 10, "italic")
    for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont", "TkCaptionFont"):
        tkfont.nametofont(name).configure(family=family, size=10)


def apply(root) -> ttk.Style:
    """Dress `root` and every ttk widget created afterwards."""
    _choose_fonts(root)
    style = ttk.Style(root)
    style.theme_use("clam")
    root.configure(background=BG)
    # the drop-down list of a Combobox is a plain tk Listbox
    root.option_add("*TCombobox*Listbox.background", FIELD)
    root.option_add("*TCombobox*Listbox.foreground", TEXT)
    root.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
    root.option_add("*TCombobox*Listbox.selectForeground", ACCENT_TEXT)
    root.option_add("*Menu.background", PANEL)
    root.option_add("*Menu.foreground", TEXT)
    root.option_add("*Menu.activeBackground", ACCENT)
    root.option_add("*Menu.activeForeground", ACCENT_TEXT)

    style.configure(".", background=BG, foreground=TEXT, fieldbackground=FIELD, bordercolor=BORDER,
                    lightcolor=BORDER, darkcolor=BORDER, troughcolor=BG, focuscolor=BG, font=FONT,
                    selectbackground=ACCENT, selectforeground=ACCENT_TEXT, insertcolor=TEXT)
    style.configure("TFrame", background=BG)
    style.configure("TLabel", background=BG, foreground=TEXT)
    style.configure("Muted.TLabel", foreground=MUTED)
    style.configure("Bad.TLabel", foreground=BAD)
    style.configure("Name.TLabel", font=FONT_BOLD)
    style.configure("Accent.TLabel", foreground=ACCENT)
    style.configure("Title.TLabel", foreground=ACCENT, font=FONT_TITLE)
    style.configure("Note.TLabel", foreground=MUTED, font=FONT_NOTE)
    style.configure("Help.TLabel", background=PANEL, foreground=TEXT, padding=10)
    style.configure("TSeparator", background=BORDER)

    style.configure("TButton", background=PANEL_2, foreground=TEXT, borderwidth=0, padding=(14, 6))
    style.map("TButton", background=[("active", BORDER), ("disabled", PANEL)], foreground=[("disabled", MUTED)])
    style.configure("Accent.TButton", background=ACCENT, foreground=ACCENT_TEXT, font=FONT_BOLD, padding=(20, 7))
    style.map("Accent.TButton", background=[("active", ACCENT_HOVER)])
    style.configure("Help.TButton", padding=(8, 3))

    style.configure("TEntry", fieldbackground=FIELD, foreground=TEXT, bordercolor=BORDER, padding=5,
                    insertcolor=TEXT)
    style.map("TEntry", bordercolor=[("focus", ACCENT)], fieldbackground=[("disabled", PANEL)],
              foreground=[("disabled", MUTED)])
    style.configure("TCombobox", fieldbackground=FIELD, background=PANEL_2, foreground=TEXT, arrowcolor=ACCENT,
                    bordercolor=BORDER, padding=5)
    style.map("TCombobox", fieldbackground=[("readonly", FIELD), ("disabled", PANEL)],
              foreground=[("disabled", MUTED)], bordercolor=[("focus", ACCENT)],
              selectbackground=[("readonly", FIELD)], selectforeground=[("readonly", TEXT)])
    style.configure("TScrollbar", background=PANEL_2, troughcolor=BG, bordercolor=BG, arrowcolor=MUTED)
    style.map("TScrollbar", background=[("active", BORDER)])

    # small tabs; the selected one grows (taller, wider, bold) instead of shrinking
    style.configure("TNotebook", background=BG, borderwidth=0, tabmargins=(0, 8, 0, 0))
    style.configure("TNotebook.Tab", background=PANEL, foreground=MUTED, padding=(11, 4), borderwidth=0, font=FONT)
    style.map("TNotebook.Tab", background=[("selected", PANEL_2), ("active", PANEL_2)],
              foreground=[("selected", ACCENT), ("active", TEXT)],
              padding=[("selected", (18, 8))], font=[("selected", FONT_BOLD)])
    return style


_BOX = dict(highlightthickness=1, highlightbackground=BORDER, highlightcolor=ACCENT, borderwidth=0, relief="flat")


def text_options() -> dict:
    """For a tk.Text (ttk styles do not reach the plain tk widgets)."""
    return dict(background=FIELD, foreground=TEXT, insertbackground=TEXT, selectbackground=ACCENT,
                selectforeground=ACCENT_TEXT, **_BOX)


def listbox_options() -> dict:
    """For a tk.Listbox."""
    return dict(background=FIELD, foreground=TEXT, selectbackground=ACCENT, selectforeground=ACCENT_TEXT, **_BOX)


def canvas_options() -> dict:
    """For the tk.Canvas behind a scrolling page."""
    return dict(background=BG, highlightthickness=0, borderwidth=0)
