# Installer prerequisites

This folder is empty in the repository on purpose. The installer carries three Microsoft installers so that a PC with none of
them can still run PrivatePartyNet offline, and they belong to Microsoft, so they are not stored here.

Run this once, before the first build:

    py tools\fetch_prereqs.py

It downloads the Microsoft Edge WebView2 runtime, the Visual C++ 2015-2022 Redistributable (x64) and the .NET Framework 4.8
offline installer from Microsoft's own links. `py tools\build.py` checks each file's Microsoft signature before it packs it.
