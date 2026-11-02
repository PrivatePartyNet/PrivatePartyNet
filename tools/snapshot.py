r"""Takes a dated snapshot of the project for the archive, and checks it against the source by checksum.

Run from the project root after each iteration:
    py tools\\snapshot.py "why this iteration exists, one line"

Each snapshot goes to a new folder named PrivatePartyNet in the archive:
    S:\Non-Game Software and Utilities\Display Management and Recording\PrivatePartyNet\Iterations\<YYYY-MM-DD>_v<version>\PrivatePartyNet\
with MANIFEST.sha256 beside it. The index in Iterations\README.txt gets a new entry, and the entry
before it is marked retired, with the snapshot that replaced it. Existing snapshots are never overwritten.
"""

import hashlib
import shutil
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from privatepartynet import __version__  # noqa: E402

ARCHIVE_ROOT = Path(r"S:\Non-Game Software and Utilities\Display Management and Recording\PrivatePartyNet")
BACKUPS = ARCHIVE_ROOT / "Iterations"
INDEX = BACKUPS / "README.txt"
LEFT_OUT_OF_SNAPSHOT = {"! LLM Files", "! Outbound", "build", "dist", "__pycache__"}


def skip_here(directory, names):
    skip = {name for name in names if name == "__pycache__" or name.endswith(".pyc")}
    if Path(directory).resolve() == ROOT:
        skip |= LEFT_OUT_OF_SNAPSHOT & set(names)
    if Path(directory).resolve() == ROOT / "installer":
        skip |= {"prereq"} & set(names)  # the WebView2 runtime is a component; it lives in the archive, not in source copies
    return skip


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def source_files():
    for path in ROOT.rglob("*"):
        relative = path.relative_to(ROOT)
        if relative.parts and relative.parts[0] in LEFT_OUT_OF_SNAPSHOT:
            continue
        if relative.parts[:2] == ("installer", "prereq"):
            continue  # the same exclusion the copy uses
        if "__pycache__" in relative.parts or path.suffix == ".pyc" or not path.is_file():
            continue
        yield path


def next_folder(name):
    candidate = BACKUPS / name
    suffix = 2
    while candidate.exists():
        candidate = BACKUPS / f"{name}_{suffix}"
        suffix += 1
    return candidate


def update_index(folder, reason, file_count):
    BACKUPS.mkdir(parents=True, exist_ok=True)
    today = date.today().isoformat()
    text = INDEX.read_text(encoding="utf-8") if INDEX.exists() else (
        "PrivatePartyNet snapshots. Each entry is one iteration. A snapshot is never overwritten.\n"
        "Retired means a newer snapshot replaced it; the entry names the replacement and the reason.\n"
    )
    if "Status: current" in text:
        head, _, tail = text.rpartition("Status: current")
        text = f"{head}Status: retired {today}, replaced by {folder.name} ({reason})" + tail
    text += (
        f"\n## {folder.name}\n"
        f"PrivatePartyNet {__version__}. Files: {file_count}. Checksums verified against the source: all {file_count}.\n"
        f"Why this snapshot: {reason}\n"
        f"Status: current\n"
    )
    INDEX.write_text(text, encoding="utf-8", newline="\n")


def main():
    if not ARCHIVE_ROOT.exists():
        sys.exit(f"Archive drive is not available: {ARCHIVE_ROOT}")
    if len(sys.argv) < 2 or not sys.argv[1].strip():
        sys.exit('Give a one-line reason: py tools\\snapshot.py "why this iteration exists"')
    reason = " ".join(sys.argv[1:]).strip()

    stamp = f"{date.today().isoformat()}_v{__version__}"
    folder = next_folder(stamp)
    destination = folder / "PrivatePartyNet"
    shutil.copytree(ROOT, destination, ignore=skip_here)

    files = list(source_files())
    manifest = []
    for source in files:
        relative = source.relative_to(ROOT)
        copied = destination / relative
        if not copied.is_file() or sha256(source) != sha256(copied):
            sys.exit(f"Checksum mismatch, snapshot is not trustworthy: {relative}")
        manifest.append(f"{sha256(copied)}  {relative.as_posix()}")
    (folder / "MANIFEST.sha256").write_text("\n".join(manifest) + "\n", encoding="utf-8", newline="\n")

    update_index(folder, reason, len(files))
    print(f"Snapshot: {folder}")
    print(f"Files copied and verified by checksum: {len(files)}")
    print(f"Index: {INDEX}")


if __name__ == "__main__":
    main()
