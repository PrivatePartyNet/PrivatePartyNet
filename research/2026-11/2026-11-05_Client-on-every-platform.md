# The PPN client on macOS, Linux, Android and iOS: window engines, voice, screen sharing, game network, phones

Who ran it: the lead (Claude Opus 5.5), in its own turns, with WebSearch (K: "I research it myself").
When: 2026-11-05, from about 10:36.
Question: K wants the client and the dedicated server "to both be able to run on as many operating systems as possible",
phones too. Today's client is Windows only: pywebview with WebView2, WebRTC for voice and screens in the page, dxcam
capture, DPAPI, a Wintun/wireguard-go game network. What does each other system offer, and what blocks it?
Where it is used: `SPEC-2026-11-05_Dedicated-server-and-mesh.md` (platforms).

"Search summary" means the search tool's summary of the pages it found, not a page read in full.

## Linux: WebKitGTK has no WebRTC in distribution builds

Search summary (wpewebkit.org/about/faq.html, FOSDEM 2026 slides by Philippe Normand of Igalia, Tauri discussion #8426,
LanternOps/breeze #9557, just-tavrn/tavrn-web #14, Aizen93/voxium #58, crabnebula-dev/tauri-plugin-webrtc, base-art.net):
- WebKitGTK and WPE moved from a GStreamer WebRTC backend to LibWebRTC. In 2.52 and 2.54 the GstWebRTC backend is
  "disabled by default and incomplete" (build flag -DUSE_GSTREAMER_WEBRTC=ON, unsupported); LibWebRTC cannot be enabled
  because the release tarballs lack its sources; the plan is LibWebRTC on by default in 2.56, "scheduled around March 2027".
- The `enable-webrtc` setting defaults to false, and Ubuntu's libwebkit2gtk-4.1 2.52 (24.04 and 26.04) is built without
  WebRTC: getUserMedia can work with enable-media-stream, but RTCPeerConnection is undefined (October 2026 report).
- Self-compiled WebKitGTK with WebRTC worked only on X11 (Wayland failed with GBM buffer errors) as of April 2025.
  FOSDEM 2026 slides: about 55% of WebRTC tests passing.
- Workarounds seen: CrabNebula's tauri-plugin-webrtc (a JavaScript shim plus a pure Rust WebRTC engine, for Tauri on
  Linux); or ship Chromium instead (the tavrn project moved its Linux app to Electron, keeping Tauri on Windows).
- Encrypted voice also needs encoded-frame transforms (RTCRtpScriptTransform), which WebKitGTK may lack.
- For PPN: pywebview's GTK backend (WebKitGTK) cannot carry PPN's voice and screens today. A Chromium-based engine on Linux
  is needed (pywebview's Qt backend uses QtWebEngine, which is Chromium; to check), or Electron/CEF.

## macOS: WKWebView has the microphone, not screen sharing

Search summary (SafeExamBrowser/seb-mac discussions #115 and #139, bradymholt/switchboard PR #21, covenant-gov/pacto-app
#363, WebKit commits, MDN, wry issues #1101 and #1195 as cited):
- getUserMedia (camera, microphone) works in WKWebView on macOS 11.1 and later. The public permission delegate
  (`requestMediaCapturePermissionFor`, macOS 12 and iOS 15, from the tool's memory) covers camera and microphone only.
- getDisplayMedia (screen sharing) is refused in WKWebView unless the app implements WebKit's private
  `_webView:requestDisplayCapturePermissionForOrigin:...`; a private API that a macOS update could change and the App Store
  may reject. NotAllowedError on Sonoma 14 and later reported in wry. System audio with a screen share is "spotty to
  unsupported". WebKit captures with ScreenCaptureKit internally.
- For PPN: pywebview's macOS backend is WKWebView, so voice could work but screens would need native capture
  (ScreenCaptureKit) fed into WebRTC, or a Chromium engine.

## iOS: no background sockets; waking an app needs Apple's push service

Search summary (Apple Developer Forums threads 40558 and 121528 and the PushKit tag, Technote 2277 as cited, getstream.io,
OneSignal docs, apnspush.com):
- The old VoIP socket keep-alive (setKeepAliveTimeout) was deprecated in iOS 9. Apple's answer to keeping a connection
  open: "There's no good way to do this on iOS. Modern VoIP apps have migrated to PushKit."
- PushKit needs APNs (Apple's push service). Since the iOS 13 SDK every VoIP push must report a CallKit call; using VoIP
  pushes to wake for chat gets an app restricted. The last unrestricted PushKit entitlement is disabled in the iOS 26 SDK.
- Background networking works only while the app is running; nothing wakes a suspended app when data arrives. Apple points
  serverless and offline apps to Local Push Connectivity, which (from the tool's memory, to check) is for specific managed
  Wi-Fi networks and needs a special entitlement.
- Who can push (search summary: Attentive, Swrve, PushEngage, AWS compute blog, Apple Developer Forums thread 772138,
  techconcepts.org, RongCloud): APNs token auth needs the bundle ID, Team ID, a .p8 signing key and its Key ID; the sender
  signs an ES256 JWT with the key; `apns-topic` must be the app's bundle ID. A .p8 key belongs to one developer team and
  pushes only to that team's apps (at most 2 active keys per account; downloadable once). Third-party push services work
  only because the app's owner uploads that key. After an app transfer the old team's key stops working for it.
  So pushes to an App Store build of PPN can come only from whoever holds the publishing team's key: the PPN project.
- For PPN: a phone cannot receive DMs or messages while PPN is closed or suspended unless something sends a push through
  APNs, which needs a provider holding Apple credentials: a server. Without one, an iPhone catches up when PPN is opened.
  This is a real conflict with "no company server" and needs K's decision (for example, an optional push relay the group
  runs, which sees only "wake up", never content).

## Qt WebEngine (Chromium inside Qt)

Search summary (doc.qt.io WebEngineView and the Qt for Python overview, Qt forum topics 113226 and 162153):
- Qt 6.7 added `desktopMediaRequested`: emitted when a page asks to capture a display; the app answers with
  selectScreen() or selectWindow() from the request's screen and window models, so the app draws its own picker (Chromium
  in Qt has no Chrome picker). Exists on QWebEnginePage for widget apps (forum); not checked whether PySide6 exposes
  QWebEngineDesktopMediaRequest, nor whether pywebview's Qt backend handles it.
- Qt 6.9 on macOS 15.3.2: the signal fires but getDisplayMedia still fails with AbortError (unanswered forum post; Screen
  Recording permission or entitlements suspected).
- Nothing found for Wayland (PipeWire portal) capture in QtWebEngine.

## Android: WebView and screens, and staying connected

Search summary (react-native-webview #231, marcusbelcher/android-getUserMedia-test, androidbugfix, LiveKit's
react-native-webrtc docs, flutter-webrtc #1521, MDN):
- getUserMedia in Android WebView works only if the app overrides `onPermissionRequest` (else a silent PermissionDenied),
  with manifest and runtime permissions and HTTPS.
- Screens: apps use MediaProjection natively (system consent dialog, a foreground service of type mediaProjection on
  Android 14 and later started before capture) with native WebRTC (react-native-webrtc, flutter-webrtc, libwebrtc).
  getDisplayMedia in Android WebView: not confirmed by these results; the tool's memory says MDN lists it unsupported in
  Chrome for Android and WebView (to check).

Search summary (developer.android.com foreground-service timeout page and Android 15 behaviour changes, GitHub issues
Rinai-R/kivotos #1 and SoftLand-Tech/Moch #100, dev.to and other 2026 guides):
- Android 14 (API 34): every foreground service declares a type (location, mediaPlayback, dataSync, phoneCall, camera,
  microphone, mediaProjection, health, remoteMessaging, systemExempted, shortService, specialUse) with its permission.
- Android 15 (target API 35): dataSync services get 6 hours in total per 24 hours (then onTimeout, a few seconds to stop,
  or a crash); no dataSync start from BOOT_COMPLETED; the budget resets when the user brings the app to the front.
  shortService is capped at 3 minutes. Android 16: jobs started from a foreground service count against job quotas.
- Apps that keep an event stream open moved to `specialUse` (no 6-hour cap, may start at boot), which needs a subtype
  property and, on Google Play, a declaration Play reviews. Google's usual advice for messaging is push (Firebase Cloud
  Messaging), which again means a server.
- For PPN: an Android phone can stay connected through a specialUse (or microphone/phoneCall during calls) foreground
  service with a visible notification; a store review decides whether that is accepted. F-Droid or a direct APK avoids
  Play's review.

## The game network (WireGuard) on each system

Search summary (github.com/WireGuard/wireguard-go README, wireguard-apple and its DeepWiki page, wireguard-apple PR #63,
manaflow-ai/cmux PR #11789, Apple Developer Forums threads 722967 and 689971, koostik-inc/undertow, HN 19186795):
- macOS, plain wireguard-go: runs on a utun device (name utun or utunN); setups run it as root (a LaunchDaemon with sudo).
- macOS and iOS, the Apple way: a Network Extension packet tunnel provider (wireguard-apple's PacketTunnelProvider with a
  C bridge to wireguard-go). Entitlement `packet-tunnel-provider`; needs a paid Apple Developer Program membership.
  A Developer ID app (outside the Mac App Store) cannot load a NetworkExtension app extension; it must use a system extension
  (`packet-tunnel-provider-systemextension` plus `com.apple.developer.system-extension.install`). iOS runs one packet tunnel
  session at a time (so PPN's game network would replace any other VPN while on).
- Android: the official tunnel library `com.wireguard.android:tunnel` (Apache-2.0, 1.0.20260102 on Maven Central): GoBackend
  is userspace wireguard-go behind Android's VpnService, no root; needs Java 8 desugaring. Its VpnService is started without
  startForeground, so an app wraps it in its own foreground service to stay up in the background. Google Play has VpnService
  disclosure rules. Android also allows one VPN at a time.
- Linux: the kernel's WireGuard or wireguard-go on a TUN device; needs CAP_NET_ADMIN (root or a capability) once to create it.
- For PPN: the game network helper today is Go (wireguard-go plus Wintun) carrying WireGuard over the app's own paths. The
  same Go code can sit behind a utun (macOS), a TUN (Linux), VpnService (Android, through gomobile or the tunnel library's
  approach) and a packet tunnel (iOS), each with its own permission step.

## Python on phones

Search summary (BeeWare November 2025 status update, Briefcase FAQ 0.3.15, briefcase discussion #958 and issue #1174,
chaquo/chaquopy #657):
- Briefcase builds iOS (Xcode project) and Android (Gradle project); Chaquopy underpins its Android support (Python 3.14
  added in late 2025). Binary packages come from Chaquopy's index (Android) and BeeWare's anaconda.org repository (iOS);
  without a mobile wheel a package with native code fails.
- November 2025: BeeWare published Rust-based iOS wheels for `cryptography` (after Maturin and PyO3 fixes), with official
  builds to follow once those land (2026 status not checked). Android has had `cryptography` on Chaquopy's index, with
  version and Python-version gaps reported earlier.
- For PPN: the Python core (store, identity, server, sync) could run on phones through Briefcase/Chaquopy if every native
  dependency has a mobile wheel (cryptography, aioquic, and others to list), but the window, voice and screens would still be
  native or WebView work per platform. The alternative is a core in a language built for every platform (Rust or Go) with
  thin per-platform shells. Needs a decision with K after the design.

## Chromium shells for the desktop: Electron and CEF

Electron, search summary (electronjs.org/docs/latest/api/desktop-capturer, electron PRs #43581 and issues #45517 and
#54628, Vesktop #1375, xdg-desktop-portal-wlr FAQ, Discord's support forum):
- Screen sharing: `session.setDisplayMediaRequestHandler()` with `desktopCapturer.getSources()`, answering
  `callback({ video: source, audio: 'loopback' })` or `callback(null)`. On Linux with PipeWire (Wayland) getSources returns
  one source chosen in the system portal's picker. `useSystemPicker` (experimental) uses the macOS system picker. System
  loopback audio works; one other application's audio does not (#54628, open).
- Pitfalls reported in 2025 and 2026: an empty callback fails with "Invalid capture constraints"; a cancelled portal gives an
  empty source list that must be answered with callback(null).
- Discord's own Linux client had broken Wayland screen sharing for years (support forum thread from about 2021); Vesktop
  (Electron) works on Wayland when the handler is right.

CEF from Python, search summary (github.com/cztomczak/cefpython README, PR #691, issues; PyPI cefpython3; Snyk):
- PyPI's cefpython3 is 66.1 (February 16th, 2021), Python up to 3.9, rated inactive. PR #691 (CEF 147, Chromium 147,
  Python 3.10 to 3.14, Windows, Linux, macOS arm64 only) appears on the main branch's README; no new PyPI release confirmed.

- For PPN, the desktop choices that carry voice and screens everywhere: (a) Electron (Chromium, desktopCapturer, PipeWire on
  Wayland) as the window, with the Python core run beside it; (b) pywebview kept on Windows (WebView2), Qt WebEngine on
  Linux and macOS (needs a spike: desktopMediaRequested from PySide6, macOS AbortError); (c) CEF through cefpython's new
  builds (young, macOS arm64 only). Electron is the best-trodden path; it also means a second runtime (Node) in the app.

## Still to research for this topic

- Keys without DPAPI: macOS Keychain, Linux Secret Service or a file, Android Keystore, iOS Keychain.
