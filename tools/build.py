"""Builds the release. Run from the project root: py tools\\build.py

Steps, and each one stops the build on failure:
  1. Unit tests (these build the game network helper and run its own Go tests too).
  2. Builds the game network helper (wghelper\\, in Go) into assets\\, next to wintun.dll, and packages the app with
     PyInstaller into dist\\PrivatePartyNet\\ (no console window).
  3. Runs PrivatePartyNet.exe --smoke-test, which serves every page over HTTPS and checks them.
  4. Builds dist\\PrivatePartyNetSetup-<version>.exe with Inno Setup.
  5. Builds dist\\PrivatePartyNet-<version>-portable.zip, for carrying on a USB stick.
  6. With --publish [FOLDER], copies both release files and their SHA-256 sums into FOLDER (default: release).

Nothing here needs the internet, once the packages from requirements.txt, the build tools, Go and the Go modules in
wghelper\\go.sum are installed.
"""

import hashlib
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(os.path.abspath(__file__)).parent.parent  # not resolve(), which would undo a subst drive (see check_path_lengths)
sys.path.insert(0, str(ROOT))

from privatepartynet import __version__  # noqa: E402

sys.path.insert(0, str(ROOT / "tools"))
import gohelper  # noqa: E402
import sign_release  # noqa: E402

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
RELEASE = ROOT / "release"
DIST = ROOT / "dist"
APP_DIR = DIST / "PrivatePartyNet"
SETUP = DIST / f"PrivatePartyNetSetup-{__version__}.exe"
PORTABLE = DIST / f"PrivatePartyNet-{__version__}-portable.zip"


def step(title):
    print(f"\n== {title}", flush=True)


def run(cmd, timeout):
    print("  $", " ".join(str(part) for part in cmd), flush=True)
    try:
        result = subprocess.run(
            [str(part) for part in cmd], cwd=ROOT, timeout=timeout,
            creationflags=NO_WINDOW, capture_output=True, text=True,
        )
    except subprocess.TimeoutExpired:
        sys.exit(f"  timed out after {timeout} seconds: {cmd[0]}")
    if result.returncode != 0:
        print(result.stdout[-4000:])
        print(result.stderr[-4000:])
        sys.exit(f"  failed with exit code {result.returncode}: {cmd[0]}")
    return result


PREREQUISITES = [
    # (file in installer\prereq, what it is, why it is needed)
    ("MicrosoftEdgeWebView2RuntimeInstallerX64.exe", "Microsoft Edge WebView2 runtime",
     "the PrivatePartyNet window is drawn by it"),
    ("vc_redist.x64.exe", "Visual C++ 2015-2022 runtime (x64)",
     "the Python runtime inside PrivatePartyNet depends on it"),
    ("Microsoft_NET_Framework_4.8_offline.exe", ".NET Framework 4.8",
     "pythonnet, which the window bridge uses, needs it on older Windows 10 PCs"),
]


def signed_by(path, who):
    """True when the file's Authenticode signature is valid and its signer's name contains `who`."""
    script = (f"$s = Get-AuthenticodeSignature -LiteralPath '{path}'; "
              "Write-Output $s.Status; Write-Output $s.SignerCertificate.Subject")
    result = subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True, text=True,
                            creationflags=NO_WINDOW, timeout=300)
    lines = result.stdout.strip().splitlines()
    return bool(lines) and lines[0].strip() == "Valid" and who in " ".join(lines[1:])


def find_prerequisites(allow_missing):
    """Every prerequisite the installer carries, each checked for a valid Microsoft signature.

    Download each from Microsoft's official source and save it in installer\\prereq:
      WebView2:  https://developer.microsoft.com/microsoft-edge/webview2 (Evergreen Standalone Installer, x64)
      VC++:      https://aka.ms/vs/17/release/vc_redist.x64.exe
      .NET 4.8:  https://go.microsoft.com/fwlink/?linkid=2088631 (the offline installer, not the web bootstrapper)
    """
    folder = ROOT / "installer" / "prereq"
    found = []
    for name, what, _why in PREREQUISITES:
        path = folder / name
        if not path.is_file():
            if allow_missing:
                print(f"  WARNING: {what} is missing ({path}). This build is not fully offline-ready.")
                continue
            sys.exit(f"Missing prerequisite: {what}\n  Save the official file as {path}\n"
                     "  or pass --allow-missing-prereqs to build without it (the installer is then not offline-ready).")
        if not signed_by(path, "Microsoft"):
            sys.exit(f"{what} at {path} does not carry a valid Microsoft signature. Not bundling it.")
        print(f"  verified {what}: {name} ({path.stat().st_size:,} bytes)")
        found.append(path)
    return found


def check_path_lengths(limit=259):
    r"""Inno Setup reads the package through installer\..\dist\, and fails with "The system cannot find the path
    specified" on a file whose path that way is longer than Windows' 260 characters. Stop first, saying what to do."""
    via = len(str(ROOT / "installer")) + len(r"\..")  # and the separator before dist, counted in relative_to below
    longest = max(DIST.rglob("*"), key=lambda p: len(str(p.relative_to(ROOT))), default=None)
    if longest is not None and via + len(str(longest.relative_to(ROOT))) > limit:
        sys.exit(f"This folder's path is too long for Inno Setup ({via + len(str(longest.relative_to(ROOT)))} characters "
                 f"to {longest.name}, over {limit}). Build from a shorter path, for example: subst P: \"{ROOT}\", "
                 r"then from P:\ run py tools\build.py (and subst P: /D afterwards).")


def find_iscc():
    candidates = [
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Inno Setup 6" / "ISCC.exe",
        Path(os.environ.get("ProgramFiles(x86)", "")) / "Inno Setup 6" / "ISCC.exe",
        Path(os.environ.get("ProgramFiles", "")) / "Inno Setup 6" / "ISCC.exe",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    sys.exit("Inno Setup 6 was not found. Install it with: winget install JRSoftware.InnoSetup")


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


PREREQ_NOTE = [
    "PrivatePartyNet needs three Microsoft prerequisites. Install each one that is missing, in this order:",
    "  1. Visual C++ runtime:     vc_redist.x64.exe",
    "  2. .NET Framework 4.8:     Microsoft_NET_Framework_4.8_offline.exe",
    "  3. WebView2 runtime:       MicrosoftEdgeWebView2RuntimeInstallerX64.exe",
    "Windows 11 normally has all three. The first two ask for permission once, because they install for every",
    "user. The WebView2 runtime installs for your user account only. Then start PrivatePartyNet.exe.",
]


def make_portable_zip(prereqs):
    with zipfile.ZipFile(PORTABLE, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(APP_DIR.rglob("*")):
            if path.is_file():
                archive.write(path, Path("PrivatePartyNet") / path.relative_to(APP_DIR))
        for path in prereqs:
            archive.write(path, Path("PrivatePartyNet") / "Prerequisites" / path.name)
        if prereqs:
            archive.writestr("PrivatePartyNet/Prerequisites/Read first.txt", "\r\n".join(PREREQ_NOTE) + "\r\n")


def write_checksums(files):
    sums = DIST / f"PrivatePartyNet-{__version__}-SHA256.txt"
    lines = [f"{sha256(source)}  {source.name}" for source in files]
    sums.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return sums


def publish(files, sums, folder):
    """Copy the release files and their checksums into a folder, replacing an earlier copy of this project's own files."""
    folder.mkdir(parents=True, exist_ok=True)
    for old in folder.glob("PrivatePartyNet*"):
        old.unlink()
    for source in [*files, sums]:
        shutil.copy2(source, folder / source.name)


def trim_opencv():
    """OpenCV ships a 30 MB video library (FFmpeg, under the LGPL) that loads only when a video is opened. PrivatePartyNet only
    encodes still pictures, so it is left out, which is smaller and leaves one less license to carry. The capture check
    that follows proves the encoder still works without it."""
    freed = 0
    for path in APP_DIR.rglob("opencv_videoio_ffmpeg*.dll"):
        freed += path.stat().st_size
        path.unlink()
    print(f"  trimmed OpenCV's video library: {freed / 1_000_000:.0f} MB")


def check_packaged_capture():
    """Run the packaged app's own capture check and read what it used. The capture packages are found by name at run
    time, so the only proof they were packaged is the packaged app using them. The check grabs real frames of this PC's
    screen, with no window, and writes one line to the log."""
    import importlib.util
    import re

    run([APP_DIR / "PrivatePartyNet.exe", "--check-capture"], timeout=120)
    log = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "PrivatePartyNet" / "privatepartynet.log"
    line = ""
    if log.exists():
        for candidate in log.read_text(encoding="utf-8", errors="replace").splitlines():
            if "Screen capture check passed" in candidate:
                line = candidate
    match = re.search(r"backend (\w+), encoder (\w+), (\d+) frames", line)
    if not match:
        sys.exit("The packaged capture check did not report what it used. Look at " + str(log))
    backend, encoder, frames = match.group(1), match.group(2), match.group(3)
    print(f"  packaged capture: {backend} backend, {encoder} encoder, {frames} frames in the check")
    if importlib.util.find_spec("cv2") and encoder != "OpenCV":
        sys.exit("OpenCV is installed here but the packaged app is encoding with Pillow: cv2 did not make it into the build.")
    if importlib.util.find_spec("dxcam") and backend != "dxgi":
        print("  WARNING: dxcam is installed here but the packaged app used the fallback capture. That is expected on a PC "
              "with no GPU output; on this PC it means dxcam or numpy did not make it into the build.")


def main():
    step("1. Unit tests")
    run([sys.executable, "-m", "unittest", "discover", "-s", "tests"], timeout=2700)  # about 20 minutes as of 3.1

    step("2. Build the game network helper and package the app with PyInstaller")
    shutil.rmtree(ROOT / "build", ignore_errors=True)
    shutil.rmtree(DIST, ignore_errors=True)
    try:
        helper = gohelper.build(ROOT / "assets" / gohelper.PROGRAM)  # assets\ goes into the package whole, below
    except RuntimeError as exc:
        sys.exit(str(exc))
    print(f"  game network helper: {helper.name} ({helper.stat().st_size:,} bytes)")
    run([
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--windowed",
        "--collect-all", "webview", "--collect-all", "clr_loader", "--collect-all", "pythonnet",
        "--collect-submodules", "mss",
        # screen capture through the graphics card, and the JPEG encoder that goes with it. dxcam is read by name at run
        # time, so its submodules and its COM bridge are collected by hand; the build's own check proves they arrived.
        "--collect-submodules", "dxcam", "--collect-submodules", "comtypes", "--hidden-import", "numpy",
        "--collect-all", "cv2",
        # the window runs on WinForms and WebView2; the other toolkits pywebview can use are not wanted, and neither is Tk
        "--exclude-module", "PyQt5", "--exclude-module", "PyQt6", "--exclude-module", "PySide2",
        "--exclude-module", "PySide6", "--exclude-module", "tkinter",
        "--name", "PrivatePartyNet",
        "--icon", ROOT / "assets" / "privatepartynet.ico",
        "--add-data", f"{ROOT / 'privatepartynet' / 'web'}{os.pathsep}privatepartynet/web",
        "--add-data", f"{ROOT / 'assets'}{os.pathsep}assets",
        "--distpath", "dist", "--workpath", "build/pyinstaller", "--specpath", "build",
        "launcher.py",
    ], timeout=900)

    trim_opencv()
    for name in (gohelper.PROGRAM, "wintun.dll"):
        if not list(APP_DIR.rglob(name)):
            sys.exit(f"{name} did not make it into the package: the game network would not work.")
    wintun = next(APP_DIR.rglob("wintun.dll"))
    if not signed_by(wintun, "WireGuard LLC"):
        sys.exit(f"{wintun} does not carry a valid WireGuard LLC signature. Not shipping it.")
    print(f"  wintun.dll: signed by WireGuard LLC ({wintun.stat().st_size:,} bytes)")

    step("3. Smoke test the packaged app")
    run([APP_DIR / "PrivatePartyNet.exe", "--smoke-test"], timeout=120)
    check_packaged_capture()
    # the window stack, which nothing else in the build reaches: the control panel opens (hidden) and its bridge answers
    run([APP_DIR / "PrivatePartyNet.exe", "--window-test"], timeout=120)

    step("4. Build the installer")
    check_path_lengths()
    prereqs = find_prerequisites(allow_missing="--allow-missing-prereqs" in sys.argv[1:])
    defines = [f"/DAppVersion={__version__}", f"/DPrereqDir={ROOT / 'installer' / 'prereq'}"]
    run([find_iscc(), *defines, ROOT / "installer" / "PrivatePartyNet.iss"], timeout=900)

    step("5. Build the portable zip")
    make_portable_zip(prereqs)

    step("6. Checksums")
    sums = write_checksums([SETUP, PORTABLE])
    for path in (SETUP, PORTABLE):
        print(f"  {path.name}  {path.stat().st_size:,} bytes  sha256 {sha256(path)}")
    print(f"  checksums: {sums}")

    step("7. Sign the update manifest")
    signed = []
    if sign_release.KEY_FILE.is_file():
        signed = list(sign_release.sign(__version__, DIST))
        for path in signed:
            print(f"  {path.name}  {path.stat().st_size:,} bytes  sha256 {sha256(path)}")
    else:
        print(f"  WARNING: no signing key at {sign_release.KEY_FILE}; this build cannot be offered as an update. "
              "Installed copies only take releases whose update.json is signed (tools/sign_release.py).")

    if "--publish" in sys.argv[1:]:
        args = sys.argv[1:]
        after = args[args.index("--publish") + 1:]
        folder = Path(after[0]) if after and not after[0].startswith("--") else RELEASE
        step(f"8. Publish to {folder}")
        publish([SETUP, PORTABLE, *signed], sums, folder)
        print(f"  copied to {folder}")
    else:
        print()
        print("Not published. Run with --publish [FOLDER] to copy the release into a folder.")
    print("\nBuild complete.")


if __name__ == "__main__":
    main()
