# Architecture

Pitch-Guard is deliberately small. Audio capture, pitch estimation, alert state,
statistics, persistence and the terminal interface are separate modules with
narrow responsibilities. The TUI never performs microphone processing on its
event loop.

## Data flow

```text
microphone / demo source thread
        │  PitchMeasurement (48–64 ms frames, no raw audio retained)
        ▼
bounded drop-oldest measurement queue
        │
        ▼
PitchGuardService.poll()  (Textual timer, ~10 Hz)
        ├── classify against the active target range
        ├── AlertTracker (monotonic continuous-episode timer)
        ├── LiveWindow (last 180 s, used by the Live chart/statistics)
        ├── History (5-minute buckets, compacted to hourly after a day)
        └── SoundDeviceBuzzer (one short "bzz" after the threshold)
        │
        ▼
dashboard, chart, statistics, periodic atomic JSON writes
```

## Modules

| Path | Responsibility |
| --- | --- |
| `audio/pitch.py` | YIN fundamental-frequency estimation, silence/noise rejection |
| `audio/capture.py` | Microphone input, device resolution, bounded queue, lifecycle |
| `audio/demo.py` | Deterministic simulated live pitch |
| `audio/demo_data.py` | In-memory seeded history for the demo dashboard |
| `audio/playback.py` | Generated buzz waveform through the default output |
| `alert.py` | Continuous out-of-range timer, cooldown and re-arm state |
| `aggregation.py` | Time buckets, histograms, compaction, daily rollups |
| `stats.py` | Live window, period windows, summaries, improvement |
| `storage.py` | Atomic JSON reads/writes and timezone-safe timestamps |
| `persistence.py` | Config/history/daily file orchestration |
| `service.py` | Connects audio, alerts, statistics and persistence |
| `ui/app.py` | Textual dashboard, timers, key bindings |
| `ui/settings.py` | Interactive settings modal with auto-save |
| `ui/chart.py` | Small dotted chart widget and numeric narrow fallback |

## Alert timing

`AlertTracker` is fed only classified voiced samples. Its state is:

- The start of the current out-of-range episode.
- Whether the current episode already produced its one allowed buzz.
- The last alert time for cooldown.
- Whether the tracker is armed.

Rules:

1. A voiced sample outside the range continues an episode if the processing gap
   from the previous out-of-range sample is at most `max_gap_s` (350 ms).
   Larger processing gaps are treated like missing audio and restart the
   episode.
2. An in-range frame ends the episode immediately.  Brief silence/unvoiced gaps
   up to 0.75 s are bridged for natural speech, but their duration is excluded
   from the elapsed threshold; longer gaps end the episode.
3. A buzz is emitted once when elapsed audio time reaches the configured
   duration. Further out-of-range samples in the same episode never buzz again.
4. After a buzz the tracker disarms. It re-arms only after a voiced in-range
   frame **and** the cooldown has elapsed.
5. If a new out-of-range episode reaches the threshold while the tracker is
   disarmed, that episode is suppressed rather than buzzing late.

All timing uses `time.monotonic()`; wall-clock time is used only for storage and
charts. This avoids clock adjustments affecting alert duration.

## Statistics model

Live data is kept at frame resolution for 180 seconds. Historical data is
aggregated into sparse pitch histograms per time bucket:

- 5-minute buckets while data is recent.
- Hourly buckets after 24 hours.
- 180-day retention so a 90-day period has a previous 90-day comparison.

Each bucket stores monitoring duration, voiced duration, in-range duration,
out-of-range duration, alert count and histogram seconds. Averages and medians
are time-weighted by voiced seconds. Partial buckets at period boundaries are
scaled by overlap, and silence never counts as a zero-Hz measurement.

The in-range percentage is stored in each bucket at capture time. Changing the
target range later therefore does not retroactively invalidate the historical
compliance value. The chart uses the current thresholds for its reference lines,
which is why the statistics note explains that in-range improvement is based on
the thresholds recorded with the data.

## Storage

```text
~/.pitch-guard/
├── config.json
├── history.json
├── daily-stats.json
└── pitch-guard.log
```

All JSON files have a `schema_version`; timestamps use UTC ISO-8601. Writes use
a temporary file, `fsync`, and atomic replacement. Corrupt files are copied to
`<name>.corrupt`, then ignored and repaired on the next save. Logs rotate at
1 MiB with two backups. Raw microphone audio is never written or retained.

## UI responsiveness

- Audio capture and pitch detection run in a daemon worker thread.
- The UI drains a small drop-oldest queue on a Textual timer.
- Statistics are computed from already-aggregated buckets; no audio frames are
  passed into widgets.
- Live chart data is tightened to terminal width before drawing.
- Live chart points are completed one-second buckets, not raw frames, so the
  newest dot does not jitter on every audio callback.
- The chart repaints only when its visible character grid actually changes.
- Alert markers use a large red dot. Live alerts carry exact wall-clock
  timestamps; historical markers are placed at the midpoint of the bucket that
  recorded the alert count.
- Narrow terminals get a numeric chart fallback.
- `--ascii` and automatic encoding detection switch borders and chart glyphs.
- The App-level event hook tracks keyboard, mouse and paste activity. After 60
  seconds without input the service is paused and an absolute-positioned overlay
  covers the chart; the next input event resumes it. Manual pause state is kept
  separate and is not auto-resumed.
