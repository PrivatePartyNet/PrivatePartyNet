"""Entry point for the packaged build. PyInstaller turns this file into PrivatePartyNet.exe.

Source users run "Start PrivatePartyNet.pyw" or "py -m privatepartynet" instead.
"""

import sys

from privatepartynet.app import main

if __name__ == "__main__":
    sys.exit(main())
