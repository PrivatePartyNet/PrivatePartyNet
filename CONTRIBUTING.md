# Contributing to PrivatePartyNet

Pull requests and issues are welcome. A contribution is licensed under the Apache License 2.0 along with the rest of the project
(section 5 of the license).

Plans for what comes next, and the research behind them, are in `research/`, one folder per month. Read
`research/README.md` first: it says how the folder is used and which plans are settled enough to build from.

## Set up

You need Windows 10 or 11, Python 3.12, Node 24 or later for the JavaScript tests and the browser test, and Go 1.27 for
the game network helper in `wghelper\` (`go test ./...` and `go build` there).

    py -m pip install -r requirements.txt
    py -m unittest discover -s tests

The unit tests include the JavaScript tests in `tests\js` (a Python test runs them when Node is installed). The three-person
browser test starts the real server and headless Microsoft Edge, and takes a few minutes:

    node tests\browser\e2e_call.mjs

Set `PRIVATEPARTYNET_PYTHON` or `PRIVATEPARTYNET_EDGE` if Python or Edge is somewhere unusual.

## Build the installer

    py tools\fetch_prereqs.py
    py tools\build.py

`fetch_prereqs.py` downloads Microsoft's three installers into `installer\prereq`. `build.py` runs the tests, packages the app,
checks that the packaged app captures the screen and opens its window, builds the installer with Inno Setup 6, and writes
checksums. It checks each Microsoft installer's signature first and stops if one does not verify.

The installer starts the app when it finishes, in a silent install too, because that is how the app's own updates run.
A scripted install that should not start it adds `/NOLAUNCH=1`: `PrivatePartyNetSetup-<version>.exe /SILENT /NOLAUNCH=1`.

## Make a release

Installed copies update themselves to the newest release on GitHub that is newer than theirs (`privatepartynet\updater.py`).
They check each file against the size and SHA-256 GitHub publishes for it, so nothing else is needed.

1. Raise the version in `privatepartynet\__init__.py` (a test version is, for example, `3.3.0-alpha.1`) and build with
   `py tools\build.py`.
2. Make the GitHub release with the tag `v<version>` (a pre-release for a test version) and upload
   `PrivatePartyNetSetup-<version>.exe` and `PrivatePartyNet-<version>-portable.zip` under exactly those names, with the
   SHA256 list. Installed copies take the installer, portable copies the zip.
3. Anyone who can publish a release can update every copy: keep two-factor sign-in on the maintainers' GitHub accounts.

## What a change needs

- A test for what changed, next to the tests that are already there. The layout and connection-stats logic are plain functions
  in `privatepartynet\web\layout.js` and `netstats.js`, tested in Node.
- If what a person sees or can do changes, update `privatepartynet\web\how.html` in the same change, and its version stamp when the
  version moves. A test fails when the stamp and `privatepartynet\__init__.py` disagree.
- Text people read is written in plain language. Say what is true once, name the thing, and skip filler.
- Match the code around your change: its comment style, its naming, its idioms.
- Nothing private in a commit: no keys, no personal paths, no real names or addresses from your own network.

## Reporting a bug

Open an issue and say which PrivatePartyNet version it is (Info, About), what Windows you use, what you did and what happened. The log
helps: Options, Open data folder, then `privatepartynet.log`. Remove anything you would rather not share before you paste it.
Security problems go through the private route in `SECURITY.md`.
