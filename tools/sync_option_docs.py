"""Writes the one-line descriptions of tools/fh_report_options.yaml into the `desc` of every option in
plugins/fh_report/schemas/fh_report_schema.yaml, and copies that schema into migrate_config.py (SCHEMA_TEXT).

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


def synced() -> tuple[str, str]:
    """(schema text, migrate_config.py text) as they should be."""
    with open(CATALOG, encoding="utf-8") as f:
        catalog = yaml.safe_load(f)
    descs = {name: opt["desc"] for name, opt in catalog["options"].items()}
    schema = described_schema(_read(SCHEMA), descs)
    return schema, with_schema_text(_read(MIGRATE), schema)


def main(argv: list[str]) -> int:
    schema, migrate = synced()
    stale = [p for p, new in ((SCHEMA, schema), (MIGRATE, migrate)) if _read(p) != new]
    if "--check" in argv:
        for p in stale:
            print("out of date:", os.path.relpath(p, ROOT))
        return 1 if stale else 0
    for p, new in ((SCHEMA, schema), (MIGRATE, migrate)):
        if p in stale:
            with open(p, "w", encoding="utf-8", newline="") as f:
                f.write(new)
            print("updated:", os.path.relpath(p, ROOT))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
