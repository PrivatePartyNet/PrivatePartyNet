# Contributing to PrivatePartyNet

Pull requests and issues are welcome. A contribution is licensed under the Apache License 2.0 along with the rest of the project
(section 5 of the license).

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

## Make a signed release

Installed copies update themselves only to releases signed with PrivatePartyNet's release key
(`privatepartynet\updater.py`, `TRUSTED_KEYS`).

1. Once: `py tools\sign_release.py --new-key` makes the key in `%APPDATA%\PrivatePartyNet-release\update-signing.key`,
   sealed for your Windows account, and prints its public key, which goes into `TRUSTED_KEYS`. To move to a new key, sign
   one release with `py tools\sign_release.py --next-key <new public key>` using the old key; copies that install it
   trust the new one too. To retire a key that leaked, sign a release with `--revoke <its public key>`: copies stop
   trusting it and every key it handed on, but never end up with no key at all. To retire the built-in key itself, put
   the new key in `TRUSTED_KEYS` in place of the old one.
2. Back the key up, once: `py tools\sign_release.py --export E:\ppn-signing-key.json` asks for a passphrase (12
   characters or more) and writes the key sealed with it. Keep the file off this PC (a USB stick in a drawer) and the
   passphrase somewhere else (a password manager). Copying the key folder is not a backup: Windows seals the key for
   this account on this PC only. Without the key no installed copy can be updated again. On a new PC, or after
   reinstalling Windows, `py tools\sign_release.py --import E:\ppn-signing-key.json` puts it back.
3. Raise the version in `privatepartynet\__init__.py` (a test version is, for example, `3.3.0-alpha.1`) and build with
   `py tools\build.py`. Its last step writes `PrivatePartyNet-<version>-update.json` and `.json.sig` into `dist\`.
4. Make the GitHub release with the tag `v<version>` (a pre-release for a test version) and upload the installer, the
   portable zip, the `update.json` and the `.json.sig`. A release without them is never offered.

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
