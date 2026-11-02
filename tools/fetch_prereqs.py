"""Downloads the three Microsoft installers that the PrivatePartyNet installer carries into installer\\prereq.

Run once before the first build: py tools\\fetch_prereqs.py

The files are Microsoft's own, from Microsoft's own links. They are not part of this repository, because they are large and
belong to Microsoft. Nothing here runs them. py tools\\build.py checks each file's Microsoft signature before it packs it,
and stops if one does not verify. A file that is already there is left alone.
"""

import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FOLDER = ROOT / "installer" / "prereq"
PER_FILE_SECONDS = 20 * 60

PREREQUISITES = [
    ("MicrosoftEdgeWebView2RuntimeInstallerX64.exe", "https://go.microsoft.com/fwlink/?linkid=2124701",
     "Microsoft Edge WebView2 runtime, Evergreen Standalone Installer (x64)"),
    ("vc_redist.x64.exe", "https://aka.ms/vs/17/release/vc_redist.x64.exe",
     "Visual C++ 2015-2022 Redistributable (x64)"),
    ("Microsoft_NET_Framework_4.8_offline.exe", "https://go.microsoft.com/fwlink/?linkid=2088631",
     ".NET Framework 4.8 offline installer"),
]


def download(url, target):
    """Fetch url to target through a temporary name, so a download that stops halfway never looks like the real file."""
    part = target.with_suffix(target.suffix + ".part")
    started = time.monotonic()
    request = urllib.request.Request(url, headers={"User-Agent": "PrivatePartyNet build tools"})
    with urllib.request.urlopen(request, timeout=60) as response, part.open("wb") as out:
        total = int(response.headers.get("Content-Length") or 0)
        done = 0
        while True:
            if time.monotonic() - started > PER_FILE_SECONDS:
                raise TimeoutError(f"{target.name} took longer than {PER_FILE_SECONDS // 60} minutes")
            chunk = response.read(1 << 20)
            if not chunk:
                break
            out.write(chunk)
            done += len(chunk)
            if total:
                print(f"\r    {done / 1e6:7.1f} of {total / 1e6:.1f} MB", end="", flush=True)
    print()
    part.replace(target)


def main():
    FOLDER.mkdir(parents=True, exist_ok=True)
    for name, url, what in PREREQUISITES:
        target = FOLDER / name
        if target.exists() and target.stat().st_size > 1_000_000:
            print(f"have  {name}")
            continue
        print(f"fetch {name}  ({what})")
        try:
            download(url, target)
        except Exception as err:
            print(f"  failed: {err}")
            print(f"  download it by hand from {url} and save it as {target}")
            return 1
    print("Done. py tools\\build.py checks each file's Microsoft signature before it packs it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
