"""Import only reviewed documentation; default is a no-write dry run."""

import argparse
import json
from pathlib import Path

from ai_powerbi_analyst.knowledge.archive import import_archive


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(json.dumps(import_archive(args.archive, args.state_root, apply=args.apply), indent=2))


if __name__ == "__main__":
    main()
