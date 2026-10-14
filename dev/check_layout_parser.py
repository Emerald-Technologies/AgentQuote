"""Build a hand-written layout parser and validate it offline on local BOM files.

Purpose: before an operator submits a parser to a live request, show that it passes the same
offline validation path a model-authored candidate takes (code policy, child-process worker,
schema, semantic and row-accounting checks). No Azure, no network, no live writes.
Inputs: --config layout JSON (bom_processor.fixed_layout_parser) or --parser an existing parse.py;
one or more --source files (.xlsx/.xls; .xlsm is checked as .xlsx, as it would have to be staged).
Outputs: the built parse.py when --write-parser is given, and one JSON summary per source on stdout
(state, lines, parts, documents, total part quantity, alternates, notes, board). Rows are printed
only with --rows (they hold customer data: never paste them into GitHub).
Run: python dev/check_layout_parser.py --config parsers/layouts/x.json --source a.xlsx [--write-parser out.py]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bom_processor.fixed_layout_parser import build_parser  # noqa: E402
from bom_processor.operator_candidate import offline_validate  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--config", type=Path)
    group.add_argument("--parser", type=Path)
    parser.add_argument("--source", type=Path, action="append", required=True)
    parser.add_argument("--write-parser", type=Path)
    parser.add_argument("--rows", action="store_true")
    args = parser.parse_args(argv)
    code = (build_parser(json.loads(args.config.read_text(encoding="utf-8"))) if args.config
            else args.parser.read_text(encoding="utf-8"))
    if args.write_parser:
        args.write_parser.write_text(code, encoding="utf-8", newline="\n")
    failed = 0
    for source in args.source:
        extension = source.suffix.lower()
        extension = ".xlsx" if extension == ".xlsm" else extension
        summary = offline_validate(source.read_bytes(), extension, code)
        rows = summary.pop("rows", [])
        summary["file"] = source.name
        if args.rows:
            summary["rows"] = rows
        else:
            summary["notes"] = [{"line": row["line_number"], "bom_notes": row["bom_notes"]}
                                for row in rows if row.get("bom_notes")]
        print(json.dumps(summary, default=str, indent=1))
        failed += summary.get("state") != "awaiting_approval"
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
