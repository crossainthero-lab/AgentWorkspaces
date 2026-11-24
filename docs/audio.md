# Audio

Every workspace gets its own audio graph unless it is created with `--audio off`. Apps in a workspace
can play sound, you can record it, and **nothing reaches your speakers** unless you explicitly route it.

## What runs

Three small processes in the workspace's systemd slice, all inside its private runtime directory
(`<workspace>/rt`):

| process | job |
|---|---|
| `pipewire` | core with one null sink, `aw-sink`, plus its monitor. Config is generated (no ALSA, Bluetooth, V4L2, JACK, portal or RT module is loaded) |
| `wireplumber --profile policy` | policy only: links app streams to `aw-sink`. The `policy` profile has no hardware monitor, so it cannot open `/dev/snd` |
| `pipewire-pulse` | PulseAudio protocol server at `<rt>/pulse/native` (Chrome, Firefox, VLC and most other apps use it) |

They run with `DBUS_SYSTEM_BUS_ADDRESS` pointing at nothing (no rtkit, no logind) and a private `HOME`/XDG
tree under `<workspace>/audio`. Measured idle cost: about 9.5 MiB PSS for the three processes together
(pipewire 2.9, wireplumber 4.3, pipewire-pulse 2.3 MiB) and no measurable CPU; creating a workspace with
audio takes about 0.4 s longer to be ready.

Apps find the server through the private `XDG_RUNTIME_DIR`. The launcher also sets `PULSE_SERVER` to the
workspace's own socket (or to a dead path when audio is off) and `PULSE_CLIENTCONFIG` with
`autospawn = no`, so libpulse can never start a real PulseAudio daemon on its own.

With `--audio off` the runtime directory has no PipeWire or Pulse socket at all. Measured: an
`ffmpeg -f pulse` client reports `Cannot connect context` and exits.

## Recording what a workspace plays

```
agent-workspaces audio-record NAME 3 -o out.wav
agent-workspaces audio-info NAME
```

API: `audio.record {workspace, seconds, path?}` -> `{path, seconds, rate: 48000, channels: 2, peak, rms,
rms_dbfs, tone_hz}`; `audio.info {workspace}` -> `{mode, sink, default_sink, daemons, streams, route}`.
Recording reads the monitor of `aw-sink` with `pw-cat`. `tone_hz` is a zero-crossing estimate and only
meaningful for a single test tone. A workspace credential may only write the WAV into
`<workspace>/shots`; the admin credential (CLI) may write anywhere it can.

## Routing to the host (off by default, human only)

```
agent-workspaces audio-route NAME host [--sink HOST_SINK_NAME]   # asks for confirmation in a terminal
agent-workspaces audio-route NAME none
```

This starts two `pw-cat` processes in the workspace's slice: one records the workspace sink, the other plays
into the host PipeWire (`$XDG_RUNTIME_DIR/pipewire-0`, or `AW_AUDIO_HOST_REMOTE`). It is the only place where
a workspace process talks to a host socket, and only on explicit request: `audio.route` needs the admin
credential **and** a human channel (an interactive terminal), exactly like grants (see
[security-model.md](security-model.md)); a workspace credential gets `E_FORBIDDEN`, an admin connection
without a terminal gets `E_NOT_HUMAN_CHANNEL`. Routing is cleared whenever the workspace restarts.

## What was verified

All of this is in `tests/integration/test_windows_audio_real.py` (real PipeWire, real clients):

* A 440 Hz tone played by `pw-cat` (native protocol) is captured from the workspace sink as 440 Hz at
  about -14 dBFS; an 880 Hz tone from `ffmpeg -f pulse` (Pulse protocol) is captured as 880 Hz; a second
  later with nothing playing the capture is exactly silent.
* The host PipeWire graph (`pw-dump`, read-only: node ids, names, classes, links) is identical before and
  after the workspace played audio; no process of the workspace holds a `/dev/snd` file descriptor.
* Routing: against a **stand-in** PipeWire instance (a second private instance, not your real one) the routed
  660 Hz tone arrives at the stand-in sink at the same level, and after `audio-route NAME none` it is
  silent. Routing to your real speakers was **not tested** (it would make noise on your machine).
* `--audio off`: no socket; a Pulse client cannot connect.

## Limits

* Output only: there is no microphone or capture source inside a workspace (the null sink's monitor is the
  only source). Nothing is recorded from the host.
* One sample format on the sink (48 kHz stereo). Apps with other formats are converted by PipeWire.
* Changing `--audio` on an existing workspace restarts its compositor (apps are closed).
* Routing to the host goes through a raw 48 kHz/s16 pipe: no latency compensation (not measured).
* Needs `pipewire`, `pipewire-pulse`, `wireplumber` and `pw-cat` installed; without them the workspace
  starts anyway and emits a `workspace.warning` event.
