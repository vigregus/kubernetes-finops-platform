"""Reads/writes the `spec.json: |` block inside a GrafanaDashboard manifest
without touching anything else in the file (header comments, labels,
instanceSelector, ...). A generic YAML round-trip (even ruamel) would still
have to special-case this block since it's a giant embedded JSON string, not
native YAML structure - doing it directly is simpler and guarantees nothing
outside the block ever changes.
"""
from __future__ import annotations

import json


def load(path: str) -> tuple[dict, list[str], int, int, int]:
    """Returns (parsed_json, all_lines, block_start, block_end, indent)."""
    with open(path) as f:
        lines = f.readlines()

    start = next(i for i, l in enumerate(lines) if l.strip() == "json: |") + 1
    indent = None
    end = len(lines)
    block_lines: list[str] = []
    for i in range(start, len(lines)):
        line = lines[i]
        if line.strip() == "":
            block_lines.append("")
            continue
        cur_indent = len(line) - len(line.lstrip(" "))
        if indent is None:
            indent = cur_indent
        if cur_indent < indent:
            end = i
            break
        block_lines.append(line[indent:])

    raw = "".join(block_lines)
    return json.loads(raw), lines, start, end, indent


def save(path: str, data: dict, lines: list[str], start: int, end: int, indent: int) -> None:
    new_json = json.dumps(data, indent=2)
    new_block_lines = [(" " * indent + line if line.strip() else line) + "\n" for line in new_json.splitlines()]
    new_lines = lines[:start] + new_block_lines + lines[end:]
    with open(path, "w") as f:
        f.writelines(new_lines)
