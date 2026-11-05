# The dedicated server on every system: starting with the system, and keeping its keys without a signed-in user

Who ran it: the lead (Claude Opus 5.5), in its own turns, with WebSearch (K: "I research it myself").
When: 2026-11-05, from about 10:40.
Question: K wants a tickbox in the dedicated console to "run the server on system startup", on as many systems as possible.
How does a Python server start at boot on Windows, Linux and macOS, and how does it protect its keys when nobody is signed
in (today's vault uses DPAPI for the signed-in user)?
Where it is used: `SPEC-2026-11-05_Dedicated-server-and-mesh.md` (the node and its console).

"Search summary" means the search tool's summary of the pages it found, not a page read in full.

## Windows: DPAPI for a service

Search summary (learn.microsoft.com CryptProtectData and its example, synacktiv's Windows secrets summary, Sygnia,
Passcape, danielcosenza.com, the billgraziano/dpapi Go library):
- `CRYPTPROTECT_LOCAL_MACHINE` (0x4) ties the data to the computer: "any user on the computer on which CryptProtectData is
  called can use CryptUnprotectData to decrypt the data", with or without extra entropy. Without the flag, only the same
  user account on the same machine can decrypt.
- SYSTEM, LocalService and NetworkService have their own user master keys (under %WINDIR%\System32\Microsoft\Protect\
  S-1-5-18\User\, sealed by the DPAPI_SYSTEM LSA secret); machine-scope keys are a separate set.
- So a service running as LocalSystem that calls CryptProtectData without the machine flag gets blobs only SYSTEM can open,
  narrower than machine scope. Add CRYPTPROTECT_UI_FORBIDDEN (0x1) for services. Keep the whole blob; never trim it.
- Machine scope "is not more secure"; Passcape warns it can expose data to every local user.
- For PPN: the node runs as a service account (LocalSystem or a dedicated virtual account) and seals its vault with that
  account's DPAPI; the console (run by any user at the machine, per K) talks to the service over a local channel the service
  authorises, never by opening the vault itself. An optional passphrase on top stays possible, but then the node cannot
  start unattended after a reboot, so it is off by default for nodes.

## Linux: systemd credentials

Search summary (systemd-creds(1) on man7.org and Ubuntu, systemd docs/CREDENTIALS.md, Charmhub, oneuptime RHEL guide,
sergeantbiggs blog):
- `systemd-creds encrypt` seals a secret with the host key (/var/lib/systemd/credential.secret, made automatically), the
  TPM2, or both ("auto": TPM2 when present and not in a container). Encrypted credentials since systemd 250.
- A unit loads it with `LoadCredentialEncrypted=name:/path` (or from /etc/credstore.encrypted/); systemd decrypts before
  starting the service into a per-service tmpfs at $CREDENTIALS_DIRECTORY, visible only to that unit, gone when it stops.
- Plain `SetCredential=` is in a world-readable unit file: public data only.
- Can bind to TPM2 PCR policies (signed PCR public key in /etc/systemd/ etc.).
- For PPN: a systemd unit for the node, its vault key in an encrypted credential (TPM2 where present), the rest of the vault
  as today. Containers and systems without systemd need a fallback (a key file readable only by the node's user, said
  plainly in the console).

## Desktop keyrings (for clients on macOS and Linux)

Search summary (pypi.org/project/keyring, keyring_jeepney, GitHub issues and PRs from taskboard, kody-bot/cli, datasure,
mynd):
- Python `keyring` uses the macOS Keychain, Windows Credential Locker and the freedesktop Secret Service (GNOME, KWallet);
  backends are entry points; `PYTHON_KEYRING_BACKEND` chooses one; the Null backend disables it.
- Headless Linux without a Secret Service falls back to the `fail` backend (set_password raises KeyringError); projects
  detect that and make the user opt in to a file or environment fallback, never a silent plaintext one. The Linux kernel
  keyring is in memory only, so secrets vanish at reboot.
- For PPN: clients on macOS use the Keychain, on Linux desktops the Secret Service, and on a headless node the systemd
  credential; a missing keyring is reported, never silently replaced.

## Starting at boot

Windows, search summary (MSSQLTips 7318 and 7325, DEV.to posts on Servy and a first Windows service, sqlpey, devgex):
- pywin32: subclass win32serviceutil.ServiceFramework; hosted by PythonService.exe; needs pywin32's post-install step;
  `python service.py start|stop|remove`. Known complaints (the tool's general knowledge, not sourced): virtual environments
  and DLL paths around pythonservice.exe.
- NSSM wraps python.exe plus a script as a service, startup Automatic. The tool's memory: last stable 2.24 from 2014.
- WinSW (XML or YAML in v3) and Servy (new, GUI and CLI, health checks, recovery) are maintained alternatives.
- A scheduled task "At startup" works but lacks supervision and restart on crash (the tool's general knowledge).
- For PPN: the packaged app is a PyInstaller exe, so the node can implement the service control protocol itself (pywin32
  inside the frozen exe) and the installer registers it; the console's tickbox switches the service between Automatic and
  Manual start. Needs a spike to confirm pywin32 services in a PyInstaller build.

macOS, search summary (theevilbit's SMAppService notes, inventivehq, mundobytes, Addigy, Apple Developer Forums threads
766351, 737038, 726864, 745412):
- LaunchDaemons (/Library/LaunchDaemons) run as root (or a named user), start before anyone logs in, no UI. LaunchAgents run
  as the user at login.
- SMAppService (macOS 13 Ventura and later) registers a login item, agent or daemon with one call; a daemon needs the user's
  approval and an administrator password; every item is listed in System Settings > General > Login Items & Extensions and
  can be switched off there. Re-register after the executable changes. Apple DTS: it does not replace installer packages and
  launchctl. Notarisation for daemons not covered by these results.
- For PPN: the node as a LaunchDaemon registered through SMAppService from the console's tickbox (with the approval prompt),
  the console as an ordinary app.

## Still to research for this topic

- The console talking to the service: a local socket or named pipe with OS-checked permissions (Windows named-pipe ACLs,
  Unix socket file permissions and peer credentials).
- Notarisation and signing needed for a macOS daemon; code signing for Windows services (SmartScreen).
