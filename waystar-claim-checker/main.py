"""Launch from this checkout without installing the local engine."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
for folder in (ROOT / "src", ROOT.parent / "Kantime-engine-main" / "src"):
    if folder.is_dir():
        sys.path.insert(0, str(folder))

from waystar_claim_checker.__main__ import main

if __name__ == "__main__":
    main()
