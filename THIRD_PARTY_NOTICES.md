# Third-party software

PrivatePartyNet is built on the software below. Each project keeps its own license, and each license text is available from the
project linked here and is included in the installer's folder for the packages that ship with one. Licenses were read from each
package's PyPI page and, for clr-loader, its GitHub repository, in October 2026. Check them again before a release.

## Python packages in the app

| Package | Version | License | Used for |
| --- | --- | --- | --- |
| [pywebview](https://github.com/r0x0r/pywebview) | 6.2.1 | BSD-3-Clause | the native windows |
| [pythonnet](https://github.com/pythonnet/pythonnet) | 3.2.1 | MIT | the bridge between Python and Windows |
| [clr-loader](https://github.com/pythonnet/clr-loader) | 0.3.1 | MIT | loads the .NET runtime for pythonnet |
| [cffi](https://github.com/python-cffi/cffi) and [pycparser](https://github.com/eliben/pycparser) | 2.0.0, 3.11 | MIT-0, BSD-3-Clause | used by clr-loader |
| [bottle](https://github.com/bottlepy/bottle) | 0.13.4 | MIT | pywebview's own helper server |
| [proxy_tools](https://github.com/jtushman/proxy_tools) | 0.1.0 | MIT | used by pywebview |
| [mss](https://github.com/BoboTiG/python-mss) | 10.2.0 | MIT | screen capture, the fallback |
| [dxcam](https://github.com/ra1nty/DXcam) | 0.3.0 | MIT | screen capture through the graphics card |
| [comtypes](https://github.com/enthought/comtypes) | 1.4.16 | MIT | dxcam's COM bridge |
| [NumPy](https://github.com/numpy/numpy) | 2.5.3 | BSD-3-Clause, with parts under 0BSD, MIT, Zlib and CC0-1.0 | frames as arrays |
| [OpenCV](https://github.com/opencv/opencv-python) (opencv-python) | 5.0.0.93 | Apache-2.0 | JPEG encoding and resizing |
| [Pillow](https://github.com/python-pillow/Pillow) | 13.0.0 | MIT-CMU | picture handling, and the fallback encoder |
| [cryptography](https://github.com/pyca/cryptography) | 50.0.2 | Apache-2.0 or BSD-3-Clause | the party's self-signed certificate |
| [psutil](https://github.com/giampaolo/psutil) | 7.2.2 | BSD-3-Clause | the CPU readout |

OpenCV's Python wheels ship the FFmpeg video library under the LGPL 2.1. PrivatePartyNet only encodes still pictures, so the build
removes that library from the installer (`opencv_videoio_ffmpeg*.dll`) and the app never loads it.

## Reaching a party through routers

| Component | License | Note |
| --- | --- | --- |
| [aioquic](https://github.com/aiortc/aioquic) 1.7.1 | BSD-3-Clause | QUIC, which carries the party between two PCs whose routers were opened by hole punching (`privatepartynet/punch.py`). |
| [pylsqpack](https://github.com/aiortc/pylsqpack) 1.0.0 | BSD-3-Clause | Comes with aioquic. |
| [pyOpenSSL](https://github.com/pyca/pyopenssl) 26.4.0 | Apache-2.0 | Comes with aioquic. |
| [service-identity](https://github.com/pyca/service-identity) 26.1.0 | MIT | Comes with aioquic. |
| [certifi](https://github.com/certifi/python-certifi) 2026.10.23 | MPL-2.0 | Comes with aioquic. PrivatePartyNet does not use its certificate list: the host's own certificate is pinned. |
| [attrs](https://github.com/python-attrs/attrs) 26.1.0 | MIT | Comes with aioquic. |

## The game network adapter

| Component | License | Note |
| --- | --- | --- |
| Wintun 0.14.1, `assets/wintun.dll` (amd64) | WireGuard LLC Prebuilt Binaries License | The unmodified, signed DLL from https://www.wintun.net/builds/wintun-0.14.1.zip (SHA-256 of the zip `07c256185d6ee3652e09fa55c0b673e2624b565e02c4b9091c79ca7d2f24ef51`, of the DLL `e5da8447dc2c320edc0fc52fa01885c103de8c118481f683643cacc3220dafce`). It may be distributed alongside software that uses it only through the API in `wintun.h`, which is how PrivatePartyNet uses it (the game network helper loads it through the wintun package below). It is not modified, and the Wintun and WireGuard names are not used to promote PrivatePartyNet. Wintun's source code is GPL-2.0 and is not part of PrivatePartyNet. |
| [wireguard-go](https://git.zx2c4.com/wireguard-go) (golang.zx2c4.com/wireguard, v0.0.0-20261024220952-b3bc704ef0ee) | MIT | WireGuard itself, built into the game network helper (`PrivatePartyNetGameNet.exe`, from `wghelper/`). Used unmodified as a library. |
| [wintun for Go](https://git.zx2c4.com/wintun-go) (golang.zx2c4.com/wintun, v0.0.0-20230126152724-0fa3db229ce2) | MIT | Loads `wintun.dll` for the helper, through the API in `wintun.h`. |
| [golang.org/x/crypto](https://pkg.go.dev/golang.org/x/crypto) 0.37.0, [x/net](https://pkg.go.dev/golang.org/x/net) 0.39.0, [x/sys](https://pkg.go.dev/golang.org/x/sys) 0.32.0 | BSD-3-Clause | Come with wireguard-go. |
| [The Go standard library](https://go.dev) | BSD-3-Clause | Built into the helper. |

## Used to build

| Tool | License | Note |
| --- | --- | --- |
| [Go](https://go.dev) 1.27.3 | BSD-3-Clause | Builds the game network helper (`tools/gohelper.py`). |
| [PyInstaller](https://pyinstaller.org) 6.21.0 | GPL-2.0-or-later with an exception | The exception lets the programs PyInstaller packages be licensed as their authors choose. See [its license page](https://pyinstaller.org/en/stable/license.html). |
| [Inno Setup](https://jrsoftware.org/isinfo.php) 6 | its own license, free to use | Makes the installer. See [its license](https://jrsoftware.org/files/is/license.txt). |

## Carried inside the installer, not part of this repository

The installer carries three Microsoft installers so that a PC with none of them can still run PrivatePartyNet offline: the Microsoft Edge
WebView2 runtime, the Visual C++ 2015-2022 Redistributable and .NET Framework 4.8. They belong to Microsoft and are covered by
Microsoft's terms. `tools/fetch_prereqs.py` downloads them from Microsoft's own links. The packaged app also contains the Python
runtime, which is under the [Python Software Foundation License](https://www.python.org/psf-landing/).
