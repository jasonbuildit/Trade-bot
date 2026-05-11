import sys
from pathlib import Path

_tests_dir = Path(__file__).parent
_wheel_dir = _tests_dir.parent

# wheel/ — so monitor, put_seller, roller, etc. resolve
sys.path.insert(0, str(_wheel_dir))
# wheel/tests/ — so `from fixtures import ...` resolves
sys.path.insert(0, str(_tests_dir))
