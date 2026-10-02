# Linux support and acceptance

The shared Python runtime and portable x86_64 ZIP support file operations, literal
search, terminal sessions and Office text read/write. This describes implementation,
not successful operation on every Linux desktop. `anywhere doctor` separates the
diagnostic process, running engine, connection catalog, authorization and acceptance.
`--state-dir` selects stored application state; it is not a filesystem sandbox or
a limit on the engine's file/terminal access. Starting `start`/`mcp` creates a
persistent OS-store authentication credential and an engine process, which requires
separate owner approval when a trial has authorized only read-only preflight.

## Check before starting

Run `anywhere doctor --state-dir /absolute/trial-state` in the intended login session.
A missing engine returns `state: stopped` and exit 1 without creating state. On Linux,
`runtime_environment.linux_prerequisites` reports the architecture and the presence
of X11/Wayland, runtime-directory and session-bus environment entries. It does not
print their values. An SSH/cloud shell may have a different environment from the
graphical desktop; absence in one process does not establish absence in another.

The credential-store check selects the same approved native backend as startup but
does not read/create credentials or request an unlock. `selection: selected` does
not mean the collection is unlocked or that agent authentication works. An unlocked
Secret Service/KWallet in the same user login session remains required. Plaintext,
file-backed and credential-free engine fallbacks are refused. A no-sudo environment
without that service needs owner/operator setup outside this change.

When `gdbus` and a session-bus address are present, the diagnostic also runs only
the bus daemon's `ListNames` and `ListActivatableNames` (two-second timeout per
call). It returns only the running/activatable flags for Secret Service and
KWallet5/6, not other bus names or addresses. A registered, stopped KWallet is
therefore distinguishable from a missing service. Failure leaves service state
unknown. It never requests activation/unlock. KWallet also needs Python's `dbus`
binding in the **selected Python runtime**; an OS utility working in a desktop
terminal does not prove that the portable Python can access that backend.

A safe follow-up for a KWallet-only desktop is to qualify `dbus-python` for the
portable interpreter/ABI and its native library dependencies in Linux packaging CI,
then verify relocation, module imports, store selection and owner-approved unlock
before creating any key. Build dependencies can exist on the packaging runner; a
target with no sudo must not be asked to modify system Python/libraries. Record
licenses/hashes and test both missing-library and unavailable/locked-wallet cases.
This patch diagnoses the missing binding; it does not add unqualified native wheels,
start KWallet or introduce another credential-storage implementation.

## User-installed Google Chrome

The isolated browser normally uses Playwright `channel=chrome` on Linux. If the
official Google Chrome package was unpacked into a trusted user directory instead
of installed system-wide, select its **ELF binary**, not the `google-chrome` shell
launcher. For example, after obtaining and checking the package from Google's
official distribution, run:

```sh
anywhere browser-configure --state-dir /absolute/trial-state \
  --chrome-executable /absolute/user-install/opt/google/chrome/chrome
anywhere browser-configure --state-dir /absolute/trial-state
anywhere doctor --state-dir /absolute/trial-state
```

Only the first command executes the selected binary, with `--version` and a five
second timeout. It requires an absolute path, a regular executable ELF file owned
by the user or root, no group/world write permission and no setuid/setgid bits.
It checks a `Google Chrome` version response and stores the resolved path, version
and SHA-256 in private `browser.json`. These checks **do not authenticate Google's
publisher signature**; the local operator must trust the downloaded package.
Validation failure preserves the previous selection.

The next `browser_open` uses that path with Chromium sandboxing enabled, checks the
saved hash, and creates a fresh ephemeral process/context. No channel fallback,
custom browser flags, `--no-sandbox`, persistent profile, CDP attachment or account
cookie import is provided. Existing owned sessions continue with their original
binary. An updated/replaced Chrome needs explicit `browser-configure` again.
Status/doctor checks metadata without executing Chrome or hashing the entire binary;
`binary_digest_verified: false` and `launch_verified: false` keep that distinction.

Restore the platform channel explicitly with:

```sh
anywhere browser-configure --state-dir /absolute/trial-state --clear-chrome-executable
```

This setting applies only to the main engine's isolated `browser_*` tools. It does
not change Subchat's dedicated/login Chrome selection. Windows keeps its installed
Edge default; macOS keeps its installed Chrome default and rejects this Linux-only
configuration command. Public beta52 does not contain this command.

Playwright is pinned to 1.58.0. Newer Chrome versions require actual navigation,
observation, input and cleanup checks; `--version` is insufficient. Playwright's
[launch API](https://playwright.dev/python/docs/api/class-browsertype#browser-type-launch)
documents custom executable selection, its compatibility limitations and the
sandbox option. Missing libraries, disallowed user namespaces or an incompatible
Chrome can still prevent launch; no host policy or permission is changed here.

From this branch's source in an independent Linux trial checkout, the following
acceptance test uses only a local HTTP fixture and temporary browser state. It
does not start the engine or create credentials:

```sh
uv sync --locked --managed-python --python 3.12
ANYWHERE_TEST_CHROME_EXECUTABLE=/absolute/user-install/opt/google/chrome/chrome \
  uv run --locked pytest -q tests/test_browser_control.py::test_configured_linux_chrome_navigation_isolation_and_input
```

The test verifies HTTP 200 navigation, observed text, fill/semantic click effects,
independent cookies, owner rejection and completed cleanup. Keep its result separate
from authenticated MCP/HTTPS and Secret Service acceptance. The native worker on
the actual desktop should perform the trial; a different cloud shell is not a
substitute for that session.
It opens/cleans up the two cookie-isolation sessions sequentially, so at most one
owned Chrome browser runs at a time. Its temporary state is not an engine or a
filesystem-access sandbox. Browser-only HTTP/MCP grants cannot supply executable
paths to `browser_open`, update this setting through `settings_update`, or invoke
the local configuration command as a tool.

## Remaining Linux features

| Feature | Current boundary | Next isolated milestone |
| --- | --- | --- |
| Native GUI (`gui_native_*`) | macOS AX helper only; Linux status says unsupported | Read-only AT-SPI application/window/tree discovery with bounded observations and stable identities |
| Native screenshots/input | No qualified Linux implementation | X11 exact-window capture/revalidation; separately test Wayland portal consent, stream lifetime and input scope |
| Audio capture | macOS helper only; Linux status says unsupported | Read-only audio-device/backend discovery, then bounded explicit microphone capture with consent |
| Office rendered preview | Uses macOS `sandbox-exec`; Linux status says unsupported | Qualify a Linux renderer sandbox with network denial and writes confined to a private workspace before running LibreOffice |
| External GUI MCP | Requires a selected provider and exact tool contract | Test that provider independently; discovery alone does not qualify native Linux GUI |

For native GUI, first inventory the target desktop/compositor, session/accessibility
bus, AT-SPI libraries and accessible apps without sending input. The
[AT-SPI API](https://gnome.pages.gitlab.gnome.org/at-spi2-core/libatspi/) provides
accessible objects, actions, text and event interfaces. A proposed adapter must
bind process/window/object identity, reject stale or ambiguous targets, use the
existing owner-scoped session/operation contracts and verify effects. Missing
accessibility trees are an unsupported result, not permission for foreground input.

Wayland requires a separate design and compositor/backend acceptance. The
[RemoteDesktop portal](https://flatpak.github.io/xdg-desktop-portal/docs/doc-org.freedesktop.portal.RemoteDesktop.html)
starts a user-approved session and grants selected input devices. The
[ScreenCast portal](https://flatpak.github.io/xdg-desktop-portal/docs/doc-org.freedesktop.portal.ScreenCast.html)
supplies selected PipeWire streams. Neither establishes an accessibility tree or
the exact-window background-input guarantee of the current macOS contract. Inspect
portal versions/backends, PipeWire availability, grants, cancellation and revocation;
do not equate X11 support with Wayland support or silently request persistent grants.

For audio, inventory PipeWire/PulseAudio/ALSA, session sockets, available input devices
and existing permissions first. Microphone and system-output capture are separate
scopes. Capture will need duration/byte limits, no automatic device switching and
confirmed stop/cleanup. No device access, permission expansion or audio recording
is introduced in this patch.

For Office preview, inventory LibreOffice/poppler plus a candidate namespace sandbox
(for example bubblewrap) and its user-namespace restrictions. Qualify confinement and
malicious OOXML cases before exposing Linux rendering; the current network/write
isolation must not be dropped merely because Linux lacks `sandbox-exec`.
