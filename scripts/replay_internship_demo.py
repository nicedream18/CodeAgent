"""Play an asciicast without extra packages; --no-wait prints for inspection."""

import argparse
import json
import time
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("cast", type=Path)
parser.add_argument("--speed", type=float, default=1)
parser.add_argument("--no-wait", action="store_true")
args = parser.parse_args()
if args.speed <= 0:
    raise SystemExit("speed must be positive")
lines = args.cast.read_text().splitlines()
header = json.loads(lines[0])
if header.get("version") != 2:
    raise SystemExit("Unsupported cast format")
started = time.monotonic()
for line in lines[1:]:
    timestamp, kind, text = json.loads(line)
    if kind == "o":
        if not args.no_wait:
            time.sleep(max(0, timestamp / args.speed - (time.monotonic() - started)))
        print(text, end="", flush=True)
