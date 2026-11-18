# Troubleshooting

## The app starts but shows `AUDIO ERROR`

Pitch-Guard deliberately keeps the dashboard running when the audio backend is
unavailable. Check the log:

- Linux: `~/.pitch-guard/pitch-guard.log`
- Custom data directory: `<data-dir>/pitch-guard.log`

### PortAudio is missing on Linux

`sounddevice` needs the native PortAudio library:

```bash
# Debian / Ubuntu
sudo apt install libportaudio2

# Fedora
sudo dnf install portaudio

# Arch
sudo pacman -S portaudio
```

On macOS and Windows, the `sounddevice` wheels normally bundle PortAudio. If the
package itself was not installed, install the audio extra/tool dependency:

```bash
uv tool install --force --from . --with sounddevice pitch-guard
```

### No input device or permissions

- **macOS:** allow the terminal application in *System Settings → Privacy &
  Security → Microphone*.
- **Windows:** allow microphone access in *Settings → Privacy → Microphone*, and
  check the default input device in *Settings → System → Sound*.
- **Linux:** make sure the terminal is not muted and that your user can access
  the microphone through PipeWire/PulseAudio. `arecord -l` lists ALSA capture
  devices.

Choose a specific device in Settings (`S`). Device names are stored; if a saved
device is disconnected, monitoring falls back to the system default and records
a warning in the log.

## No alert buzz

- The alert only fires after one **continuous** out-of-range episode of the
  configured duration. Silence, unvoiced input, and in-range pitch reset it.
- The cooldown must have elapsed, and the pitch must have returned in range
  after the previous buzz before the tracker re-arms.
- Check the saved volume in Settings (0 mutes it).
- `--mute` disables the buzz without changing the saved volume.
- Confirm the default output device works. Playback errors are logged once and
  never stop monitoring.

## The chart says “Chart hidden at this size”

The chart falls back to a numeric summary when the widget is too small. Enlarge
the terminal, or reduce the font size. The dashboard reflows to a two-column
layout on narrow windows.

## Borders or chart characters look wrong

Force the ASCII fallback:

```bash
pitch-guard --ascii
```

or set `PITCH_GUARD_ASCII=1`. The app also falls back automatically when the
terminal encoding cannot represent box-drawing characters.

## Reset local data

Configuration and statistics are plain JSON in `~/.pitch-guard`. Stop the app,
then remove or move that directory. No accounts, cloud sync, or telemetry are
involved.

To try the interface without touching real history:

```bash
pitch-guard --demo
```

## Demo screenshots/GIF

The committed README demo is generated from the actual Textual app:

```bash
uv run python scripts/capture_demo.py
```

It needs Pillow (dev dependency) and `rsvg-convert` (librsvg) or ImageMagick.
The script drives demo mode, switches periods through the real key bindings,
exports real screenshots, and writes `docs/demo.gif`.

## Tested environments

The implementation and automated tests were exercised on Fedora Linux with a
UTF-8 terminal and Textual 8.2. The demo dashboard and the headless UI tests were
run on that machine. Real microphone capture could not be tested in this
environment because PortAudio was not installed and no audio input was exposed;
the app handles that condition gracefully. macOS and Windows were not available
for execution testing, so no compatibility claim is made beyond the
cross-platform libraries and code paths used here.
