# Native audio capture experiment

This is an experimental macOS 15+ helper, not an installed Anywhere Computer
capability or a supported virtual microphone driver. Windows is not implemented.

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

## Verification on 2026-09-19

- A first three-second experiment exceeded the requested system-audio duration
  while capture startup was pending. The helper now caps saved samples.
- A subsequent real capture saved exactly three seconds per track, but both
  tracks measured zero peak/RMS. Audible content was not verified.
- The user reported selection of an iPhone microphone. Default-input capture
  was removed; no further microphone capture was performed after that report.
- Three tests in `tests/test_audio_probe.py` pass without opening a capture
  device. Two reject missing microphone IDs before creating an output folder.
  A synthetic PCM harness crosses the duration boundary for both tracks and
  independently reopens the files to check frame counts and signal metrics.
- Removing final-buffer clamping in a temporary source copy makes that harness
  fail. The repository source was preserved during this negative test.

A later system-only experiment played a quiet synthetic 440 Hz WAV through
`afplay` and captured two seconds with `microphone_device_id=none`. It returned
96,000 frames at 48 kHz, peak 0.04883 and RMS 0.03447. Independent conversion
with `afconvert` and Python WAV reading confirmed two seconds of stereo audio;
positive zero crossings estimated 441.5 Hz. This confirms non-silent OS playback
capture, not physical speaker output. Playback and recording both ended.

Virtual-device routing, device disconnect,
Windows support, and MCP/operation-ledger integration remain pending. Do not
advertise this experiment as a shipped audio feature.
