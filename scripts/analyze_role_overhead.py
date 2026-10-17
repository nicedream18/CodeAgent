"""Analyze an existing canonical trace/archive without invoking models/tools."""

import argparse
import json
from pathlib import Path

from mokioclaw.evaluation.overhead import analyze_overhead, read_events

parser = argparse.ArgumentParser()
parser.add_argument("trace", type=Path)
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()
report = analyze_overhead(read_events(args.trace))
args.output.parent.mkdir(parents=True, exist_ok=True)
args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
print(json.dumps({"requests": report["requests"], "last_requested_role": report["last_requested_role"],
                  "output": str(args.output)}))
