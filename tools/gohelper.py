"""Builds the game network helper (wghelper/, in Go) into PrivatePartyNetGameNet.exe.

The release build puts it next to wintun.dll in assets\\ (tools\\build.py), and the tests build their own copy. From a
source checkout, build it once into assets\\ so the game network works when run from source:
    py tools\\gohelper.py

Needs Go at the version wghelper\\go.mod names, or later (https://go.dev/dl/; the zip unpacked anywhere works). The build
is reproducible: -trimpath leaves no folder names of this PC in the program.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "wghelper"
PROGRAM = "PrivatePartyNetGameNet.exe"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def find_go():
    """go.exe from GOROOT, the PATH, or a per-user install in %LOCALAPPDATA%\\Programs\\go. None when there is none."""
    candidates = []
    if os.environ.get("GOROOT"):
        candidates.append(Path(os.environ["GOROOT"]) / "bin" / "go.exe")
    on_path = shutil.which("go")
    if on_path:
        candidates.append(Path(on_path))
    candidates.append(Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "go" / "bin" / "go.exe")
    candidates.append(Path(os.environ.get("ProgramFiles", "")) / "Go" / "bin" / "go.exe")
    return next((c for c in candidates if c.is_file()), None)


def build(output, go=None, timeout=600):
    """Build the helper into `output` (a file path). Returns the path. Raises RuntimeError when Go is missing or the build
    fails."""
    go = go or find_go()
    if go is None:
        raise RuntimeError("Go was not found. Install it from https://go.dev/dl/ (the zip unpacked into "
                           "%LOCALAPPDATA%\\Programs\\go works without administrator rights).")
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    # -H windowsgui: no console window when Windows starts it; -s -w: no debug tables
    result = subprocess.run([str(go), "build", "-trimpath", "-ldflags", "-s -w -H windowsgui", "-o", str(output), "."],
                            cwd=SOURCE, capture_output=True, text=True, timeout=timeout, creationflags=NO_WINDOW,
                            env={**os.environ, "CGO_ENABLED": "0", "GOOS": "windows", "GOARCH": "amd64"})
    if result.returncode != 0:
        raise RuntimeError(f"the game network helper did not build:\n{result.stdout}{result.stderr}")
    return output


def test(go=None, timeout=900):
    """Run the helper's own Go tests. Raises RuntimeError when they fail."""
    go = go or find_go()
    if go is None:
        raise RuntimeError("Go was not found.")
    result = subprocess.run([str(go), "test", "-count=1", "./..."], cwd=SOURCE, capture_output=True, text=True,
                            timeout=timeout, creationflags=NO_WINDOW, env={**os.environ, "CGO_ENABLED": "0"})
    if result.returncode != 0:
        raise RuntimeError(f"the game network helper's tests failed:\n{result.stdout[-4000:]}{result.stderr[-4000:]}")
    return result.stdout


if __name__ == "__main__":
    try:
        print(build(ROOT / "assets" / PROGRAM))
    except RuntimeError as exc:
        sys.exit(str(exc))
