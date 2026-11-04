"""Writes what tools/fh_report_options.yaml says into the files that repeat it:
  - the one-line `desc` of every option in plugins/fh_report/schemas/fh_report_schema.yaml, and that schema
    inside migrate_config.py (SCHEMA_TEXT);
  - the comment block at the top of fh_report.yaml: HEADER_COMMENT in migrate_config.py and the header of
    config/plugins/fh_report.yaml.

    python tools/sync_option_docs.py          rewrite the two files
    python tools/sync_option_docs.py --check  change nothing, exit 1 if they are out of date

A test runs the check, so an option is described once, in the catalog.
"""
import os
import re
import sys

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CATALOG = os.path.join(ROOT, "tools", "fh_report_options.yaml")
SCHEMA = os.path.join(ROOT, "plugins", "fh_report", "schemas", "fh_report_schema.yaml")
MIGRATE = os.path.join(ROOT, "migrate_config.py")
EXAMPLE = os.path.join(ROOT, "config", "plugins", "fh_report.yaml")

_FLOW = re.compile(r"^(\s+)(\w+): (\{.*\})(\s+#.*)?$")
_BLOCK = re.compile(r"^(\s+)(\w+):\s*$")
_DESC_IN_FLOW = re.compile(r",\s*desc: '(?:[^']|'')*'")


def _quote(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def _read(path: str) -> str:
    with open(path, encoding="utf-8", newline="") as f:
        return f.read()


def described_schema(schema_text: str, descs: dict) -> str:
    """The schema text with `desc` set on each option of element_schema that the catalog describes."""
    lines = schema_text.split("\n")
    start = lines.index("schema;element_schema:")
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith(("schema;", "type:"))), len(lines))
    out, i = lines[:start + 1], start + 1
    while i < end:
        line = lines[i]
        m = _FLOW.match(line)
        if m and m.group(2) in descs and len(m.group(1)) == 4:
            body = _DESC_IN_FLOW.sub("", m.group(3))
            body = body[:-1] + ", desc: " + _quote(descs[m.group(2)]) + "}"
            out.append(f"{m.group(1)}{m.group(2)}: {body}{m.group(4) or ''}")
        elif (m := _BLOCK.match(line)) and m.group(2) in descs and len(m.group(1)) == 4:
            out.append(line)
            i += 1
            if lines[i].strip().startswith("desc:"):          # replace the old description
                i += 1
            out.append(f"{m.group(1)}  desc: {_quote(descs[m.group(2)])}")
            continue
        else:
            out.append(line)
        i += 1
    return "\n".join(out + lines[end:])


def with_schema_text(migrate_text: str, schema_text: str) -> str:
    marker = "SCHEMA_TEXT = r'''"
    a = migrate_text.index(marker) + len(marker)
    b = migrate_text.index("'''", a)
    return migrate_text[:a] + schema_text + migrate_text[b:]


def load_catalog() -> dict:
    with open(CATALOG, encoding="utf-8") as f:
        return yaml.safe_load(f)


def render_header(catalog: dict) -> str:
    """The comment block of fh_report.yaml: the free text blocks and the `help` of each option, in the
    catalog's `header` order, each kept exactly as written."""
    parts = []
    for block in catalog["header"]:
        parts.append(block["text"] if "text" in block else catalog["options"][block["option"]]["help"])
    return "\n".join(parts) + "\n"


_HEADER_MARKER = 'HEADER_COMMENT = """'


def header_in(migrate_text: str) -> str:
    a = migrate_text.index(_HEADER_MARKER) + len(_HEADER_MARKER)
    return migrate_text[a:migrate_text.index('"""', a)]


def with_header(migrate_text: str, header: str) -> str:
    a = migrate_text.index(_HEADER_MARKER) + len(_HEADER_MARKER)
    return migrate_text[:a] + header + migrate_text[migrate_text.index('"""', a):]


def synced() -> dict:
    """{path: text} of the files this script writes, as they should be."""
    catalog = load_catalog()
    descs = {name: opt["desc"] for name, opt in catalog["options"].items()}
    schema = described_schema(_read(SCHEMA), descs)
    migrate_now = _read(MIGRATE)
    old_header, header = header_in(migrate_now), render_header(catalog)
    migrate = with_header(with_schema_text(migrate_now, schema), header)
    example = _read(EXAMPLE)
    assert example.startswith(old_header.strip()), "config/plugins/fh_report.yaml does not start with the header"
    example = header.strip() + example[len(old_header.strip()):]
    return {SCHEMA: schema, MIGRATE: migrate, EXAMPLE: example}


def main(argv: list[str]) -> int:
    wanted = synced()
    stale = [p for p, new in wanted.items() if _read(p) != new]
    if "--check" in argv:
        for p in stale:
            print("out of date:", os.path.relpath(p, ROOT))
        return 1 if stale else 0
    for p in stale:
        with open(p, "w", encoding="utf-8", newline="") as f:
            f.write(wanted[p])
        print("updated:", os.path.relpath(p, ROOT))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
