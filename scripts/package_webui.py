"""Package the already-built WebUI into immutable release assets."""

import argparse
import json
from pathlib import Path

from backend.core.webui_distribution import ROOT, build_identity, package_distribution


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = package_distribution(
        ROOT / "web/dist", args.output.resolve(), build_identity()
    )
    print(json.dumps({key: str(value) for key, value in result.items()}))


if __name__ == "__main__":
    main()
