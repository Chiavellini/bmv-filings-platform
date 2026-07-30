import sys
from pathlib import Path

EARNINGS_ROOT = Path(__file__).resolve().parents[1]
if str(EARNINGS_ROOT) not in sys.path:
    sys.path.insert(0, str(EARNINGS_ROOT))
