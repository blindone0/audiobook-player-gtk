#!/usr/bin/python3
"""Find the narrator's pauses in every audio file and write player/phrases.json.

The player uses these as sentence/phrase starts: when you resume, it goes back to the start of the
sentence you stopped in, and `R` replays the last sentence.

usage: tools/scan_pauses.py [audio_dir] [out_json]      (defaults: audio/  player/phrases.json)

Decodes with GStreamer (gst-launch-1.0) at 8 kHz mono and measures loudness in 20 ms frames.
A pause is >= 240 ms quieter than ~22 dB below the file's typical speech level; for each pause it
stores [time where speech resumes, pause length] in seconds. Needs Python <= 3.12 (audioop).
"""
import json
import os
import subprocess
import sys
import warnings
from concurrent.futures import ThreadPoolExecutor

with warnings.catch_warnings():
    warnings.simplefilter('ignore', DeprecationWarning)
    import audioop  # removed in Python 3.13

RATE, FRAME = 8000, 160          # 20 ms frames


def scan(path):
    p = subprocess.Popen(['gst-launch-1.0', '-q', 'filesrc', f'location={path}', '!', 'decodebin', '!', 'audioconvert', '!',
                          'audioresample', '!', f'audio/x-raw,format=S16LE,channels=1,rate={RATE}', '!', 'fdsink', 'fd=1'],
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    rms, buf = [], b''
    while True:
        chunk = p.stdout.read(FRAME * 2 * 500)
        if not chunk:
            break
        buf += chunk
        n = len(buf) // (FRAME * 2) * (FRAME * 2)
        rms.extend(audioop.rms(buf[i:i + FRAME * 2], 2) for i in range(0, n, FRAME * 2))
        buf = buf[n:]
    p.wait()
    if not rms:
        raise RuntimeError(f'could not decode {path}')
    speech = sorted(rms)[int(len(rms) * 0.70)] or 1
    quiet = max(speech * 0.08, 30)
    pauses, run = [], 0
    for i, v in enumerate(rms):
        if v < quiet:
            run += 1
        else:
            if run >= 12:
                pauses.append([round(i * 0.02, 2), round(run * 0.02, 2)])
            run = 0
    return pauses


def main():
    adir = sys.argv[1] if len(sys.argv) > 1 else 'audio'
    out = sys.argv[2] if len(sys.argv) > 2 else os.path.join('player', 'phrases.json')
    files = sorted(f for f in os.listdir(adir) if f.lower().endswith(('.mp3', '.m4a', '.ogg', '.opus', '.flac')))
    with ThreadPoolExecutor(max_workers=os.cpu_count() or 2) as ex:
        results = list(ex.map(lambda f: scan(os.path.join(adir, f)), files))
    data = {'about': 'speech onsets after narrator pauses, per file: [seconds, pause_length]',
            'files': {f'{i:02d}': r for i, r in enumerate(results, 1)}}
    json.dump(data, open(out, 'w'), separators=(',', ':'))
    print(f'{len(files)} files, {sum(map(len, results))} pauses -> {out}')


if __name__ == '__main__':
    main()
