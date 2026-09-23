# Native audio capture experiment

This is an experimental macOS 15+ helper with an optional engine integration.
It is not a virtual microphone driver. Windows is not implemented, and source
integration does not establish installation in a published or running package.

Compile `scripts/probe_audio_capture.swift` with `swiftc -parse-as-library`.
Use `--check` to inspect existing permissions and `--list-devices` to list audio
inputs. Neither command starts a recording or requests permission.

Capture syntax: `probe system|microphone|both SECONDS NEW_OUTPUT_DIRECTORY
[MIC_DEVICE_ID]`. The directory must be absolute and new, and the duration must
be between 1 and 30 seconds. Microphone modes require an explicit device ID;
they never intentionally select the default input. Device names alone do not
prove that an input is virtual or that a desired signal is routed to it. The
behavior when a selected device disappears during capture remains unverified.

System mode uses ScreenCaptureKit audio with microphone capture disabled. It
does not create an input endpoint for other applications. This prototype needs
existing screen-capture permission in every mode, and existing microphone
permission for microphone modes. It does not change system input settings.

Separate CAF files contain each requested source, with sample-count limits and
peak/RMS measurements. Wall-clock startup/shutdown may exceed the requested
duration. Source timestamps are not yet aligned. A captured file does not prove
audible speaker output; silence is a valid recording, not a playback success.
Failures can leave partial files in the private output directory.

## Optional portable packaging

On the macOS build machine, compile the reviewed helper for the target architecture:

```sh
swiftc -parse-as-library scripts/probe_audio_capture.swift -o /tmp/anywhere-audio
python scripts/build_portable.py --runtime /absolute/standalone-python \
  --output /absolute/new-portable.zip --audio-helper /tmp/anywhere-audio
```

The helper is optional and is copied to `native/anywhere-audio` inside the portable
directory. Its bytes are included in the existing `manifest.json` file hashes.
The build rejects non-macOS hosts, relative paths, symlinks and non-executable
files. The builder must supply a trusted binary for the target architecture;
file validation is not code-signature or architecture verification. This option
does not request permissions or record audio. The current engine exposes the
system-capture tool below; permission onboarding and native signing remain pending.

## Read-only engine inspection

`audio_status` inspects the optional helper under the portable interpreter's
`../native/anywhere-audio` path. It first checks the executable against that
installation's manifest, then invokes only `--check` and `--list-devices`.
Each query has a ten-second deadline and a 64 KiB output limit; timed-out or
oversized responses fail and the owned child is terminated and reaped.

The tool reports `unsupported` outside macOS and `unavailable` when the helper
is absent. A valid inspection reports permissions and named input IDs, always
with `capture_started=false`, `capture_tool_available=true` and
`capture_sources=["system"]`. Availability does not mean permission is granted. It neither
requests permissions nor chooses a default input. The helper's presence is not
proof of an audio signal or of permission to record. A manifest digest checks
installation consistency, not publisher authentication. Existing HTTP grants still determine tool access.


## Experimental system capture through the engine

`audio_capture` accepts `source="system"`, an integer `seconds` between 1 and 30,
and an absolute `output_directory` that does not yet exist under an existing
parent. It requires the manifest-verified optional helper and already-granted
screen-capture permission. Microphone capture is not exposed by this tool; it
never selects an input device or requests permission.

Use a unique operation ID for the authorized recording. The existing engine
ledger handles duplicate calls and recovery with `operations_get`, including
after engine restart. Never use a new ID merely because a recording response
was lost. A helper failure or invalid receipt after dispatch yields `unknown`
and may leave partial files; those files are preserved for inspection.

The result includes the CAF path, byte count and SHA-256, plus frame count,
sample rate, duration and peak/RMS reported by the native helper. The engine
checks receipt consistency, rejects symlink artifacts and checks the CAF magic;
it does not independently decode the audio. `measurements_source=native_helper`
and `speaker_output_verified=false` preserve that boundary. Silence is valid.

The new engine and HTTP/MCP acceptance tests use a synthetic helper, never an
actual recording device. They verify one dispatch for duplicate operations,
restart recovery, permission refusal, partial failures and invalid artifacts.
Real capture through the packaged engine, microphone device-loss behavior and
Windows capture remain separate acceptance work.
