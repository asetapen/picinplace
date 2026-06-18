import sys
from pathlib import Path

# Make server.py (repo root) importable from tests/.
sys.path.insert(0, str(Path(__file__).parent))
