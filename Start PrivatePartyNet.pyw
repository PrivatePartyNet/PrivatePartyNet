"""Double-click to open the PrivatePartyNet host window. A .pyw file runs under pythonw, so no console window appears."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from privatepartynet.app import main  # noqa: E402

main()
