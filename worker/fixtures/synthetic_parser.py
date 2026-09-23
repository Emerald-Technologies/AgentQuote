import csv
import json
import sys

args = sys.argv
input_path = args[args.index("--input") + 1]
output_path = args[args.index("--output") + 1]
with open(input_path, newline="", encoding="utf-8") as source:
    rows = list(csv.DictReader(source))
result = {"schema": "agentquote.synthetic.v1", "rows": rows}
with open(output_path, "w", encoding="utf-8", newline="\n") as target:
    json.dump(result, target, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
