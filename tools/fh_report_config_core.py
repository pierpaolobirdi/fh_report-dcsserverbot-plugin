"""Core of the Fh_Report configuration editor: everything except the window.

It reads config/plugins/fh_report.yaml keeping its comments and order (ruamel.yaml, the library
DCSServerBot itself uses), knows every option from tools/fh_report_options.yaml (sections, texts,
defaults) and from the schema the plugin ships (types, ranges), checks values the way that schema does,
and saves with a backup. tools/fh_report_config.pyw is the window on top of it.
"""
from __future__ import annotations

import io
import os
import re
import tempfile
import textwrap
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import yaml
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap
from ruamel.yaml.constructor import DuplicateKeyError
from ruamel.yaml.error import MarkedYAMLError

TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
CATALOG_FILE = os.path.join(TOOLS_DIR, "fh_report_options.yaml")
CONFIG_PARTS = ("config", "plugins", "fh_report.yaml")
SCHEMA_PARTS = ("plugins", "fh_report", "schemas", "fh_report_schema.yaml")
RESERVED = {"DEFAULT", "commands", "chat_commands"}
BACKUPS_KEPT = 10

UNSET = object()          # "take this option out of the block"


class ConfigError(Exception):
    """Something the person using the editor has to read as it is."""


# ── Where things are ─────────────────────────────────────────────────────────────────────────

@dataclass
class Install:
    root: str                     # the DCSServerBot folder
    config_file: str
    schema_file: str | None       # the schema the installed plugin ships (None: use the repository's)


def find_installation(path: str) -> Install:
    """`path` is the DCSServerBot folder, the plugin's config file, or a folder inside the installation."""
    path = os.path.abspath(path)
    if os.path.isfile(path):
        if os.path.basename(path) != CONFIG_PARTS[-1]:
            raise ConfigError(f"{path} is not fh_report.yaml.")
        root = os.path.dirname(os.path.dirname(os.path.dirname(path)))
        return Install(root, path, _schema_in(root))
    here = path
    for _ in range(4):
        candidate = os.path.join(here, *CONFIG_PARTS)
        if os.path.isfile(candidate):
            return Install(here, candidate, _schema_in(here))
        parent = os.path.dirname(here)
        if parent == here:
            break
        here = parent
    raise ConfigError(f"No config{os.sep}plugins{os.sep}fh_report.yaml found in or above {path}.")


def _schema_in(root: str) -> str | None:
    installed = os.path.join(root, *SCHEMA_PARTS)
    if os.path.isfile(installed):
        return installed
    shipped = os.path.join(os.path.dirname(TOOLS_DIR), *SCHEMA_PARTS)
    return shipped if os.path.isfile(shipped) else None


# ── What each option is ──────────────────────────────────────────────────────────────────────

@dataclass
class OptionSpec:
    name: str
    section: str
    kind: str                      # bool, int, text, multi, list, weekdays
    desc: str
    help: str                      # the explanation, as plain text
    since: str
    default: Any = None
    required: bool = False
    default_only: bool = False
    example: str | None = None
    min: int | None = None
    max: int | None = None
    pattern: str | None = None
    nullable: bool = True
    schema_type: str = "str"       # str: text only; text: text or a number
    func: str | None = None
    weekdays: tuple = ()


def plain_help(help_text: str, name: str) -> str:
    """The comment text of the file header without its `#` marks, its alignment and the `name - ` start."""
    lines = [line[1:] if line.startswith("#") else line for line in help_text.split("\n")]
    first = re.sub(rf"^\s*{re.escape(name)}\s+- ", "", lines[0])
    rest = textwrap.dedent("\n".join(lines[1:])).rstrip() if len(lines) > 1 else ""
    return (first.rstrip() + ("\n" + rest if rest else "")).strip()


class Model:
    """The catalog (sections, texts) together with the schema (types, ranges)."""

    def __init__(self, schema_file: str | None = None, catalog_file: str = CATALOG_FILE):
        with open(catalog_file, encoding="utf-8") as f:
            catalog = yaml.safe_load(f)
        schema_file = schema_file or os.path.join(os.path.dirname(TOOLS_DIR), *SCHEMA_PARTS)
        with open(schema_file, encoding="utf-8") as f:
            schema = yaml.safe_load(f)
        self.rules: dict = schema["schema;element_schema"]["mapping"]
        self.sections: list[dict] = catalog["sections"]
        section_of = {name: s["id"] for s in self.sections for name in s["options"]}
        self.options: dict[str, OptionSpec] = {}
        for name, entry in catalog["options"].items():
            self.options[name] = self._spec(name, entry, section_of[name], self.rules.get(name) or {})

    @staticmethod
    def _spec(name: str, entry: dict, section: str, rule: dict) -> OptionSpec:
        func = rule.get("func")
        kind = {"bool": "bool", "int": "int", "seq": "list", "map": "weekdays"}.get(rule.get("type"), "text")
        if func in ("int_or_list", "text_or_list", "str_or_list"):
            kind = "multi"
        rng = rule.get("range") or {}
        return OptionSpec(
            name=name, section=section, kind=kind, desc=entry["desc"], since=entry["since"],
            help=plain_help(entry["help"], name), default=entry.get("default"),
            required=bool(entry.get("required")), default_only=bool(entry.get("default_only")),
            example=entry.get("example"), min=rng.get("min"), max=rng.get("max"),
            pattern=rule.get("pattern"), nullable=rule.get("nullable", True) is not False, func=func,
            schema_type=rule.get("type", "str"),
            weekdays=tuple((rule.get("mapping") or {}).keys()) if kind == "weekdays" else ())

    def in_section(self, section_id: str) -> list[OptionSpec]:
        section = next(s for s in self.sections if s["id"] == section_id)
        return [self.options[n] for n in section["options"]]


# ── Values: what is typed, what is stored, what is accepted ─────────────────────────────────

def check_value(spec: OptionSpec, value: Any) -> str | None:
    """Why `value` would not pass the plugin's schema, or None when it does."""
    if value is None:
        return None if spec.nullable else "needs a value, or remove the line"
    if spec.kind == "bool":
        return None if isinstance(value, bool) else "must be true or false"
    if spec.kind == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            return "must be a whole number"
        if spec.min is not None and value < spec.min:
            return f"must be at least {spec.min}"
        if spec.max is not None and value > spec.max:
            return f"must be at most {spec.max}"
        return None
    if spec.kind == "multi":
        items = value if isinstance(value, list) else [value]
        for item in items:
            if spec.func == "int_or_list" and (isinstance(item, bool) or not isinstance(item, int)):
                return "must be a whole number, or a list of them"
            if spec.func == "text_or_list" and (isinstance(item, bool) or not isinstance(item, (int, str))):
                return "must be a number or text, or a list of them"
            if spec.func == "str_or_list" and not isinstance(item, str):
                return "must be text, or a list of text"
        return None
    if spec.kind == "list":
        if not isinstance(value, list) or not all(isinstance(i, str) for i in value):
            return "must be a list of text"
        return "has a repeated entry" if len(set(value)) != len(value) else None
    if spec.kind == "weekdays":
        if not isinstance(value, dict):
            return "must list days (mon to sun) with a time"
        bad = [d for d in value if d not in spec.weekdays]
        if bad:
            return f"unknown day {bad[0]!r} (use {', '.join(spec.weekdays)})"
        return None
    allowed = (str, int) if spec.schema_type == "text" else (str,)
    if isinstance(value, bool) or not isinstance(value, allowed):
        return "must be text"
    if spec.pattern and not re.search(spec.pattern, str(value)):
        return f"is not valid{' (e.g. ' + spec.example + ')' if spec.example else ''}"
    return None


def value_to_input(spec: OptionSpec, value: Any) -> Any:
    """What a field starts with for a stored value: bool stays, others become text; weekdays stay a dict."""
    if value is None or value is UNSET:
        return {} if spec.kind == "weekdays" else ""
    if spec.kind == "bool":
        return bool(value)
    if spec.kind == "multi":
        return ", ".join(str(v) for v in (value if isinstance(value, list) else [value]))
    if spec.kind == "list":
        return "\n".join(str(v) for v in value)
    if spec.kind == "weekdays":
        return {str(k): str(v) for k, v in dict(value).items()}
    return str(value)


def parse_input(spec: OptionSpec, typed: Any) -> tuple[Any, str | None]:
    """(value to store, problem). The value is UNSET when the field was left empty."""
    if spec.kind == "bool":
        return bool(typed), None
    if spec.kind == "weekdays":
        days = {d: str(v).strip() for d, v in dict(typed or {}).items() if str(v).strip()}
        value = days or UNSET
        return value, (check_value(spec, days) if days else None)
    text = str(typed if typed is not None else "").strip()
    if not text:
        return UNSET, None
    if spec.kind == "int":
        try:
            value = int(text)
        except ValueError:
            return UNSET, "must be a whole number"
    elif spec.kind == "multi":
        items = [i.strip() for i in re.split(r"[,\n]", text) if i.strip()]
        if spec.func == "int_or_list":
            try:
                items = [int(i) for i in items]
            except ValueError:
                return UNSET, "must be a whole number, or several separated by commas"
        elif spec.func == "text_or_list":
            items = [int(i) if re.fullmatch(r"\d+", i) else i for i in items]
        value = items[0] if len(items) == 1 else items
    elif spec.kind == "list":
        value = [i.strip() for i in text.split("\n") if i.strip()]
    else:
        value = text
    problem = check_value(spec, value)
    return (UNSET, problem) if problem else (value, None)


# ── The file ─────────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Target:
    """A block of the file: DEFAULT, a server (flat) or a server under its node."""
    key: tuple

    @property
    def label(self) -> str:
        return " / ".join(self.key) if self.key != ("DEFAULT",) else "DEFAULT (every server)"

    @property
    def is_default(self) -> bool:
        return self.key == ("DEFAULT",)


@dataclass
class Problem:
    where: str
    option: str | None
    message: str
    level: str = "error"          # error, warning

    def __str__(self) -> str:
        return f"{self.where}: {self.option + ' ' if self.option else ''}{self.message}"


def _round_trip() -> YAML:
    y = YAML()
    y.preserve_quotes = True
    y.indent(mapping=2, sequence=4, offset=2)
    y.width = 10 ** 6
    return y


class ConfigFile:
    def __init__(self, text: str, model: Model, path: str | None = None):
        self.model, self.path = model, path
        self.newline = "\r\n" if "\r\n" in text else "\n"
        self._yaml = _round_trip()
        try:
            self.data = self._yaml.load(text.replace("\r\n", "\n"))
        except DuplicateKeyError as e:
            mark = e.problem_mark
            line = f"line {mark.line + 1}: " if mark else ""
            raise ConfigError(f"{line}{e.problem or 'a key appears twice'}. Delete one of the two lines.") from e
        except MarkedYAMLError as e:
            mark = e.problem_mark
            line = f"line {mark.line + 1}: " if mark else ""
            raise ConfigError(f"{line}{e.problem or 'the file is not valid YAML'}") from e
        if self.data is None:
            self.data = CommentedMap()
        if not isinstance(self.data, dict):
            raise ConfigError("The file must be a list of blocks (DEFAULT and one per server).")

    @classmethod
    def open(cls, path: str, model: Model) -> "ConfigFile":
        with open(path, "rb") as f:
            return cls(f.read().decode("utf-8-sig"), model, path)

    # structure ----------------------------------------------------------------------------

    def _is_node(self, name: str, block: Any) -> bool:
        return (name not in RESERVED and isinstance(block, dict) and bool(block)
                and not (set(block) & set(self.model.options))
                and all(isinstance(v, dict) for v in block.values()))

    def targets(self) -> list[Target]:
        found = [Target(("DEFAULT",))] if "DEFAULT" in self.data else []
        for name, block in self.data.items():
            if name in RESERVED:
                continue
            if self._is_node(name, block):
                found += [Target((name, inst)) for inst in block]
            else:
                found.append(Target((name,)))
        return found

    def block(self, target: Target, create: bool = False):
        node: Any = self.data
        for part in target.key:
            if part not in node or node[part] is None:
                if not create:
                    return None
                node[part] = CommentedMap()
            node = node[part]
        return node

    # reading and changing ---------------------------------------------------------------------

    def get(self, target: Target, option: str) -> tuple[Any, str]:
        """(value, where it comes from): own, DEFAULT or default (the plugin's own)."""
        own = self.block(target)
        if own is not None and option in own:
            return own[option], "own"
        if not target.is_default:
            default = self.block(Target(("DEFAULT",)))
            if default is not None and option in default:
                return default[option], "DEFAULT"
        return self.model.options[option].default, "default"

    def set(self, target: Target, option: str, value: Any) -> None:
        if option not in self.model.options:
            raise ConfigError(f"Unknown option {option}.")
        block = self.block(target, create=True)
        if value is UNSET:
            block.pop(option, None)
        elif isinstance(value, dict):
            current = block.get(option)
            new = current if isinstance(current, CommentedMap) else CommentedMap()
            for day in list(new):
                if day not in value:
                    del new[day]
            for day, v in value.items():
                new[day] = v
            block[option] = new
        else:
            block[option] = value

    def add_server(self, name: str, channel_id: int, campaign_name: str, node: str | None = None) -> Target:
        name = name.strip()
        if not name or any(c in name for c in ":#\n"):
            raise ConfigError("The server name is the instance name of DCSServerBot (for example DCS.release_server).")
        target = Target((node.strip(), name) if node else (name,))
        if self.block(target) is not None:
            raise ConfigError(f"{target.label} is already in the file.")
        block = self.block(target, create=True)
        block["channel_id"] = channel_id
        block["campaign_name"] = campaign_name
        return target

    # checking -------------------------------------------------------------------------------

    def validate(self) -> list[Problem]:
        problems: list[Problem] = []
        default = self.block(Target(("DEFAULT",))) or {}
        for target in self.targets():
            block = self.block(target) or {}
            where = target.label
            for option, value in block.items():
                spec = self.model.options.get(option)
                if spec is None:
                    rule = self.model.rules.get(option) or {}
                    note = rule["enum"][0] if rule.get("func") == "deprecated" and rule.get("enum") else \
                        "is not an option of this plugin"
                    problems.append(Problem(where, option, note, "warning"))
                    continue
                issue = check_value(spec, value if not isinstance(value, dict) else dict(value))
                if issue:
                    problems.append(Problem(where, option, issue))
                elif spec.default_only and not target.is_default:
                    problems.append(Problem(where, option, "is only read from DEFAULT", "warning"))
            if not target.is_default:
                for need in ("channel_id", "campaign_name"):
                    if need not in block and need not in default:
                        problems.append(Problem(where, need, "is missing (the plugin needs it for this server)"))
        return problems

    # saving ---------------------------------------------------------------------------------

    def dumps(self) -> str:
        buf = io.StringIO()
        self._yaml.dump(self.data, buf)
        text = buf.getvalue()
        return text.replace("\n", "\r\n") if self.newline == "\r\n" else text

    def save(self, path: str | None = None) -> str | None:
        """Write the file (atomically), keeping the old one as <name>.<date>.bak. Returns the backup path."""
        path = path or self.path
        if not path:
            raise ConfigError("There is no file to save to.")
        text = self.dumps()
        ConfigFile(text, self.model)                       # what is written must read back
        backup = None
        if os.path.exists(path):
            backup = f"{path}.{datetime.now():%Y%m%d-%H%M%S}.bak"
            with open(path, "rb") as src, open(backup, "wb") as dst:
                dst.write(src.read())
            self._prune_backups(path)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(text.encode("utf-8"))
            os.replace(tmp, path)
        except Exception:
            if os.path.exists(tmp):
                os.remove(tmp)
            raise
        self.path = path
        return backup

    @staticmethod
    def _prune_backups(path: str) -> None:
        folder, base = os.path.split(path)
        old = sorted(f for f in os.listdir(folder or ".") if f.startswith(base + ".") and f.endswith(".bak"))
        for name in old[:-BACKUPS_KEPT]:
            try:
                os.remove(os.path.join(folder, name))
            except OSError:
                pass
