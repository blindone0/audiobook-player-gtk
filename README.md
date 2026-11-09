# Audiobook player: «Град обреченный»

A low-power native desktop player for one long audiobook, built around the problem of **not missing any text**. It was set up for the Strugatskys' novel «Град обреченный» (6 parts, 19 chapters, 61 MP3 files, 17 h 44 min), but the book is just a data file (`player/book.js`).

> **This repository contains no audio, no text and no artwork from the book.** You need your own copy of the recording. What's here is the player, the chapter map (file numbers, durations, chapter titles) and pause timestamps measured from the recording.

## Features

- **Native GTK 3 window** that follows your system theme. It plays through GStreamer: no browser, no Electron.
- **Low power.** Measured on an i7-4710HQ laptop:

  | State | CPU use | Wakeups/s |
  |---|---|---|
  | Playing, window minimized | 0.5% of one core | 56 |
  | Playing, window visible | 1.2% | 64 |
  | Paused | 0% | 1 |

  The window updates once a second, and only while it's visible and playing. Chapter changes and the sleep timer use one-shot timers. The audio buffer is large (1 s), and only audio plugins are loaded.
- **Resume at the start of the sentence.** The position (file + second) is saved every 20 s while playing, and immediately on pause, any jump, quit, and before suspend. On start, and when you press play after a pause of more than a minute, playback goes back to the start of the sentence you stopped in. Sentence starts are the narrator's pauses, detected in the audio (`player/phrases.json`).
- **History of every rewind and jump.** Each rewind and jump has a **Back** button, repeated key presses merge into one entry, and listening stretches are logged with times. Resumes, sleep-timer stops and suspends are listed too. `Ctrl+Z` undoes the last jump.
- **Navigation:** chapters (parts → chapters), bookmarks with notes, `R` to replay the last sentence.
- Speed 0.5–3× without pitch change, a sleep timer (minutes or end of chapter, with a fade-out), its own volume control, and seamless playback from one file to the next.
- **Media keys and the desktop sound menu** work through MPRIS. Before suspend it pauses and saves, holding a logind *delay* lock so the save always completes. Idle suspend is blocked only while playing.
- An optional **browser version** (`./play.sh --web`) shares the same `progress.json`.

## Setup

Debian / Ubuntu / Linux Mint:

```bash
sudo apt install python3-gi gir1.2-gtk-3.0 gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0 gstreamer1.0-plugins-base gstreamer1.0-plugins-good
```

Put the recording's files in `audio/` as `01.mp3 … 61.mp3`, in book order. Optionally, add `player/cover.jpg` (cover art) and `player/icon.png` (window and launcher icon). Then:

```bash
./play.sh
```

Desktop launcher (menu, desktop, panel):

```bash
sed "s|@DIR@|$PWD|g" grad-obrechennyj-audiobook.desktop.in > ~/.local/share/applications/grad-obrechennyj-audiobook.desktop
```

The window's class is `grad-obrechennyj-audiobook`, so the panel groups it under a pinned launcher.

## Keys

`Space` play/pause · `←`/`→` −15 s / +30 s · `Shift+←/→` previous / next chapter · `R` repeat sentence · `Ctrl+Z` undo jump · `↑`/`↓` volume · `[` `]` speed · `B` bookmark · `M` mute · `Ctrl+Q` quit

## Files

| Path | What |
|---|---|
| `player/gradplayer.py` | the native player (single file, Python + PyGObject) |
| `player/book.js` | the book: title, files with exact durations, chapter start points (file, second) |
| `player/phrases.json` | sentence starts: narrator pauses per file, `[seconds, pause length]` |
| `tools/scan_pauses.py` | regenerates `phrases.json` from your audio files |
| `player/index.html`, `player/server.py` | browser version, with a tiny localhost server that saves to `progress.json` |
| `play.sh` | launcher: native by default, `--web` for the browser version |
| `progress.json` | created locally: position, speed, volume, bookmarks, history. Not in the repo. |

## Using it for another audiobook

1. Write `player/book.js` with your files (`"file"`, exact `"dur"` in seconds) and chapters (`"track"`, `"time"`, `"part"`, `"title"`). To get exact durations, decode each file rather than trusting the duration in its header.
2. Run `tools/scan_pauses.py audio player/phrases.json`.
3. Optionally, change `WMCLASS` and `APP_ID` at the top of `gradplayer.py`, and rename the `.desktop.in` to match.
