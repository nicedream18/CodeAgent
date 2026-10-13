from __future__ import annotations

import argparse
from pathlib import Path

from mokioclaw.core.daemon import AgentDaemon


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the MokioClaw local Agent daemon.")
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    AgentDaemon(args.state_dir, host=args.host, port=args.port).run()


if __name__ == "__main__":
    main()
