"""Install the three paired wheels into this Python's prepared Agent runtime."""
from pathlib import Path
import hashlib
import json
import subprocess
import sys

root = Path(__file__).resolve().parent
if sys.platform != "linux" or sys.version_info[:2] != (3, 12):
    raise SystemExit("Run with the destination Agent runtime's Linux Python 3.12")
manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
wheels = []
for item in manifest["wheels"]:
    wheel = root / item["file"]
    assert hashlib.sha256(wheel.read_bytes()).hexdigest() == item["sha256"], "Wheel checksum mismatch"
    wheels.append(str(wheel))
subprocess.run([sys.executable, "-m", "pip", "install", "--no-index", "--no-deps", "--force-reinstall", *wheels], check=True)
subprocess.run([sys.executable, str(root / "verify_install.py")], check=True)
