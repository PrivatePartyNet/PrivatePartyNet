import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {"dist", "build", ".git", "! LLM Files", "prereq", "__pycache__", "node_modules"}
TEXT = {".py", ".pyw", ".js", ".mjs", ".html", ".css", ".md", ".txt", ".iss", ".json", ".yml", ".yaml", ".toml"}
# Names the project used before it was called PrivatePartyNet. They are written in pieces so this file does not contain
# them. A pattern for a prefix of the old names (the old backup extension, the old invite code, the old script objects)
# is included for the same reason.
OLD_NAMES = re.compile("chil" + "lery|stoo" + "p|chill" + r"(?=bak|1-|Chat|Voice)", re.I)
# A path on someone's own PC, or an email address, does not belong in the repository.
PERSONAL = re.compile(r"[A-Za-z]:\\+Users\\+[A-Za-z0-9_.-]+|[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PERSONAL_ALLOWED = {"LICENSE"}


def published_names():
    """The files git would publish (not ignored), or None when git or the repository is not there."""
    try:
        out = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=ROOT, capture_output=True,
                             timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return [name for name in out.stdout.decode("utf-8", "replace").split("\0") if name]


def project_files():
    names = published_names()
    if names is not None:
        for name in names:
            if (ROOT / name).is_file():
                yield ROOT / name, Path(name)
        return
    for path in ROOT.rglob("*"):
        relative = path.relative_to(ROOT)
        if not any(part in SKIP_DIRS for part in relative.parts):
            yield path, relative


def text_files():
    for path, relative in project_files():
        if path.is_file() and (path.suffix.lower() in TEXT or path.name in {"NOTICE", "LICENSE", ".gitignore"}):
            yield path, relative


class RenameSweepTests(unittest.TestCase):
    """The app is called PrivatePartyNet. No file, folder or line of the repository carries an earlier name."""

    def test_no_file_or_folder_is_named_after_an_old_app(self):
        stray = [str(rel) for _, rel in project_files() if OLD_NAMES.search(str(rel))]
        self.assertEqual(stray, [])

    def test_the_old_names_stay_out_of_the_text_files(self):
        hits = []
        for path, relative in text_files():
            for number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                if OLD_NAMES.search(line):
                    hits.append(f"{relative.as_posix()}:{number}")
        self.assertEqual(hits, [], "an old app name is still in the source")

    def test_the_data_folder_has_the_current_name(self):
        text = (ROOT / "privatepartynet" / "paths.py").read_text(encoding="utf-8")
        self.assertIn('APP_FOLDER = "PrivatePartyNet"', text)

    def test_no_personal_path_or_email_is_in_the_text_files(self):
        hits = []
        for path, relative in text_files():
            if relative.as_posix() in PERSONAL_ALLOWED:
                continue
            for number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                if PERSONAL.search(line):
                    hits.append(f"{relative.as_posix()}:{number}")
        self.assertEqual(hits, [], "a personal path or email address is in a file that will be published")


if __name__ == "__main__":
    unittest.main()
