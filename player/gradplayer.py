#!/usr/bin/python3
"""Град обреченный — native audiobook player (GTK 3 + GStreamer, no browser).

Power: playback is GStreamer (mpg123 decoder, large audio buffer); the window redraws once a second
only while it is visible and playing; position is written every 20 s while playing and on every
pause / jump / quit / suspend; chapter changes and the sleep timer use one-shot timers, not polling.

Never miss text: on start, and when you press play after a pause, playback goes back to the start of
the sentence you stopped in (narrator pauses detected in the audio, player/phrases.json). Every
rewind, jump, listening stretch and sleep-timer stop is kept in the History tab; each entry can take
you back to where you were. Ctrl+Z undoes the last jump.

Progress is shared with the browser player: ../progress.json.
"""
import bisect
import json
import os
import shutil
import signal
import sys
import time
import types

import gi
gi.require_version('Gtk', '3.0')
gi.require_version('Gdk', '3.0')
gi.require_version('Gst', '1.0')
gi.require_version('GdkPixbuf', '2.0')
from gi.repository import GLib, Gio, Gdk, GdkPixbuf, Gst, Gtk  # noqa: E402

WMCLASS = 'grad-obrechennyj-audiobook'          # = .desktop file name, so the panel groups the window
APP_ID = 'local.audiobook.GradObrechennyj'
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
STATE_FILE = os.environ.get('AUDIOBOOK_STATE') or os.path.join(ROOT, 'progress.json')
TEST = bool(os.environ.get('AUDIOBOOK_TEST_CMDS'))
SPEEDS = [0.5, 0.6, 0.7, 0.75, 0.8, 0.85, 0.9, 0.95, 1, 1.05, 1.1, 1.15, 1.2, 1.25, 1.3, 1.4, 1.5, 1.6, 1.75, 2, 2.25, 2.5, 3]
SLEEPS = [('Sleep: off', 0), ('Sleep: 5 min', 5), ('Sleep: 10 min', 10), ('Sleep: 15 min', 15), ('Sleep: 30 min', 30),
          ('Sleep: 45 min', 45), ('Sleep: 60 min', 60), ('Sleep: 90 min', 90), ('Sleep: end of chapter', -1)]
ROMAN = ['I', 'II', 'III', 'IV', 'V', 'VI']
SAVE_EVERY = 20          # seconds, while playing
RESUME_LEAD = 1.5        # go this much before the stop point, then back to the sentence start
LONG_PAUSE = 60          # pause longer than this -> rewind to sentence start on play


def now_ms():
    return int(time.time() * 1000)


def fmt(s):
    s = max(0, int(s + 0.0001))
    h, m, x = s // 3600, s % 3600 // 60, s % 60
    return f'{h}:{m:02d}:{x:02d}' if h else f'{m}:{x:02d}'


def fmt_long(s):
    s = max(0, int(round(s)))
    h, m = s // 3600, s % 3600 // 60
    return f'{h} h {m} min' if h else f'{m} min'


def ordinal_number(title):
    for i, w in enumerate(['перв', 'втор', 'трет', 'четв', 'пят', 'шест'], 1):
        if w in title.lower():
            return i
    return 0


# ---------------------------------------------------------------- book data
class Book:
    def __init__(self):
        raw = open(os.path.join(HERE, 'book.js'), encoding='utf-8').read()
        b = json.loads(raw.split('window.BOOK = ', 1)[1].rstrip().rstrip(';'))
        self.title, self.authors, self.reader = b['title'], b['authors'], b['reader']
        self.cover = os.path.join(HERE, b['cover'])
        self.tracks = b['tracks']
        self.starts, acc = [], 0.0
        for t in self.tracks:
            self.starts.append(acc)
            acc += t['dur']
        self.total = acc
        self.chapters = []
        part_no = 0
        last_part = None
        for c in b['chapters']:
            if c['part'] and c['part'] != last_part:
                part_no += 1
                last_part = c['part']
            g = self.starts[c['track'] - 1] + c['time']
            if not c['part']:
                code = 'Intro'
            elif c['title'] == c['part'].split('. ', 1)[-1] or not ordinal_number(c['title']):
                code = ROMAN[part_no - 1]
            else:
                code = f'{ROMAN[part_no - 1]}.{ordinal_number(c["title"])}'
            self.chapters.append(dict(c, g=g, code=code))
        for i, c in enumerate(self.chapters):
            c['end'] = self.chapters[i + 1]['g'] if i + 1 < len(self.chapters) else self.total
        self.ch_starts = [c['g'] for c in self.chapters]
        try:
            ph = json.load(open(os.path.join(HERE, 'phrases.json')))['files']
            self.phrases = {int(k): v for k, v in ph.items()}
        except (OSError, ValueError, KeyError):
            self.phrases = {}

    def path(self, n):
        return os.path.join(ROOT, self.tracks[n - 1]['file'])

    def locate(self, g):
        g = max(0.0, min(self.total - 0.05, g))
        n = bisect.bisect_right(self.starts, g)
        return n, g - self.starts[n - 1]

    def chapter_at(self, g):
        return max(0, bisect.bisect_right(self.ch_starts, g + 0.05) - 1)

    def label(self, g, short=True):
        c = self.chapters[self.chapter_at(g)]
        if short:
            return f'{c["code"]} · {fmt(g - c["g"])}'
        return f'{c["part"] + " — " if c["part"] else ""}{c["title"]}, {fmt(g - c["g"])}'

    def sentence_start(self, g, lead=RESUME_LEAD, window=20.0):
        """Start of the sentence/phrase around g - lead (a narrator pause), never more than `window` back."""
        n, t = self.locate(g)
        target = t - lead
        p = self.phrases.get(n, [])
        if target <= 0.3 or not p:
            return self.starts[n - 1] + max(0.0, target)
        ons = [o for o, ln in p if target - window <= o <= target]
        good = [o for o, ln in p if target - window <= o <= target and ln >= 0.6]
        pick = (good or ons or [None])[-1]
        if pick is None:
            return self.starts[n - 1] + target
        return self.starts[n - 1] + max(0.0, pick - 0.12)

    def next_sentence(self, g):
        n, t = self.locate(g)
        for o, ln in self.phrases.get(n, []):
            if o > t + 0.5:
                return self.starts[n - 1] + max(0.0, o - 0.12)
        return self.starts[n] if n < len(self.tracks) else g


# ---------------------------------------------------------------- state file
class State:
    def __init__(self, book):
        self.book = book
        self.d = self._read(STATE_FILE) or self._read(STATE_FILE + '.bak') or {}
        d = self.d
        d.setdefault('track', 1)
        d.setdefault('time', 0.0)
        d.setdefault('speed', 1)
        d.setdefault('volume', 1)
        d.setdefault('bookmarks', [])
        d.setdefault('history', [])
        d.setdefault('events', [])
        d.setdefault('listenedSeconds', 0)
        if not 1 <= d['track'] <= len(book.tracks):
            d['track'], d['time'] = 1, 0.0
        if os.path.exists(STATE_FILE):        # one-generation backup of the previous session
            try:
                shutil.copyfile(STATE_FILE, STATE_FILE + '.bak')
            except OSError:
                pass
        self.saved_at = None

    @staticmethod
    def _read(p):
        try:
            d = json.load(open(p, encoding='utf-8'))
            return d if isinstance(d, dict) and d.get('track') else None
        except (OSError, ValueError):
            return None

    @property
    def g(self):
        return self.book.starts[self.d['track'] - 1] + self.d['time']

    def set_pos(self, g):
        n, t = self.book.locate(g)
        self.d['track'], self.d['time'] = n, round(t, 2)

    def save(self, durable=False):
        d = self.d
        try:
            rev = json.load(open(STATE_FILE, encoding='utf-8')).get('rev', 0)
        except (OSError, ValueError):
            rev = d.get('rev', 0)
        d['rev'] = max(rev, d.get('rev', 0)) + 1
        d['updated'] = now_ms()
        tmp = STATE_FILE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
            if durable:
                f.flush()
                os.fsync(f.fileno())
        os.replace(tmp, STATE_FILE)
        if durable:
            try:
                fd = os.open(os.path.dirname(STATE_FILE), os.O_RDONLY)
                os.fsync(fd)
                os.close(fd)
            except OSError:
                pass
        self.saved_at = time.time()

    # history of jumps / listening (kept in progress.json "events")
    def event(self, kind, **kw):
        ev = dict(kind=kind, at=now_ms(), **kw)
        E = self.d['events']
        last = E[-1] if E else None
        if (kind == 'seek' and last and last['kind'] == 'seek' and last.get('reason') == kw.get('reason')
                and ev['at'] - last.get('last', last['at']) < 4000):
            last['to'] = kw['to']                       # repeated presses of the same key -> one entry
            last['last'] = ev['at']
            last['count'] = last.get('count', 1) + 1
        else:
            E.append(ev)
        del E[:-400]
        if kind in ('seek', 'chapter', 'bookmark', 'file', 'history', 'book') and 'from' in kw:
            if kind != 'seek' or abs(kw['to'] - kw['from']) > 60:   # browser player's "recent positions"
                self.d['history'] = ([{'g': kw['from'], 'at': ev['at']}] + self.d['history'])[:15]


# ---------------------------------------------------------------- GStreamer engine
class Engine:
    """playbin (audio only) -> scaletempo -> pulsesink. Gapless across files."""

    def __init__(self, book, on_state, on_track, on_error, on_eos):
        self.book = book
        self.cb_state, self.cb_track, self.cb_error, self.cb_eos = on_state, on_track, on_error, on_eos
        self.pipe = Gst.ElementFactory.make('playbin', 'player')
        self.pipe.set_property('flags', 0x2)            # audio only: no video/subtitle plugins loaded
        sinkname = os.environ.get('AUDIOBOOK_SINK', 'pulsesink')
        sink = Gst.ElementFactory.make(sinkname, 'sink')
        if sinkname == 'pulsesink':
            sink.set_property('client-name', book.title)
            sink.set_property('buffer-time', 1_000_000)     # 1 s buffer, 100 ms periods: few wakeups
            sink.set_property('latency-time', 100_000)
        elif sinkname == 'fakesink':
            sink.set_property('sync', True)
        self.pipe.set_property('audio-sink', sink)
        tempo = Gst.ElementFactory.make('scaletempo', 'tempo')   # speed without chipmunk voice
        if tempo:
            self.pipe.set_property('audio-filter', tempo)
        self.pipe.connect('about-to-finish', self._about_to_finish)
        bus = self.pipe.get_bus()
        bus.add_signal_watch()
        bus.connect('message', self._on_message)
        self.track = 0              # file currently loaded / playing
        self.queued = None          # next file queued for gapless switch
        self.pending = None         # (seconds, play) to apply once prerolled
        self.rate = 1.0
        self.playing = False
        self.last_pos = 0.0

    def uri(self, n):
        return Gst.filename_to_uri(self.book.path(n))

    def load(self, n, t, play):
        self.queued = None
        self.pipe.set_state(Gst.State.READY)
        self.pipe.set_property('uri', self.uri(n))
        self.track, self.pending, self.last_pos = n, (t, play), t
        self.pipe.set_state(Gst.State.PAUSED)

    def position(self):
        if self.pending:
            return self.pending[0]
        ok, pos = self.pipe.query_position(Gst.Format.TIME)
        if ok and pos >= 0:
            self.last_pos = pos / Gst.SECOND
        return self.last_pos

    def seek(self, t):
        if self.pending:
            self.pending = (t, self.pending[1])
            return
        self.last_pos = t
        self.pipe.seek(self.rate, Gst.Format.TIME, Gst.SeekFlags.FLUSH | Gst.SeekFlags.ACCURATE,
                       Gst.SeekType.SET, int(t * Gst.SECOND), Gst.SeekType.NONE, -1)

    def set_rate(self, r):
        self.rate = float(r)
        if self.track and not self.pending:
            self.seek(self.position())

    def play(self):
        if self.pending:
            self.pending = (self.pending[0], True)
        else:
            self.pipe.set_state(Gst.State.PLAYING)

    def pause(self):
        if self.pending:
            self.pending = (self.pending[0], False)
        self.pipe.set_state(Gst.State.PAUSED)

    def stop(self):
        self.pipe.set_state(Gst.State.NULL)

    def _about_to_finish(self, pipe):            # streaming thread
        n = self.track
        if self.queued is None and n < len(self.book.tracks):
            self.queued = n + 1
            pipe.set_property('uri', self.uri(n + 1))

    def _on_message(self, bus, msg):
        t = msg.type
        if t == Gst.MessageType.ASYNC_DONE and self.pending:
            pos, play = self.pending
            self.pending = None
            if pos > 0.05 or self.rate != 1.0:
                self.seek(pos)
            if play:
                self.pipe.set_state(Gst.State.PLAYING)
        elif t == Gst.MessageType.STATE_CHANGED and msg.src == self.pipe:
            old, new, pending = msg.parse_state_changed()
            if new == Gst.State.PAUSED and pending == Gst.State.PLAYING:
                return                        # transient during a seek while playing, not a real pause
            playing = new == Gst.State.PLAYING
            if playing != self.playing:
                self.playing = playing
                self.cb_state(playing)
        elif t == Gst.MessageType.STREAM_START and self.queued:
            self.track, self.queued, self.last_pos = self.queued, None, 0.0
            if self.rate != 1.0:
                GLib.idle_add(lambda: self.seek(self.position()) and False)
            self.cb_track(self.track)
        elif t == Gst.MessageType.EOS:
            self.cb_eos()
        elif t == Gst.MessageType.ERROR:
            err, dbg = msg.parse_error()
            self.cb_error(f'{err.message}')


# ---------------------------------------------------------------- MPRIS (media keys, panel sound menu)
MPRIS_XML = '''<node>
<interface name="org.mpris.MediaPlayer2">
 <method name="Raise"/><method name="Quit"/>
 <property name="CanQuit" type="b" access="read"/><property name="CanRaise" type="b" access="read"/>
 <property name="HasTrackList" type="b" access="read"/><property name="Identity" type="s" access="read"/>
 <property name="DesktopEntry" type="s" access="read"/>
 <property name="SupportedUriSchemes" type="as" access="read"/><property name="SupportedMimeTypes" type="as" access="read"/>
</interface>
<interface name="org.mpris.MediaPlayer2.Player">
 <method name="Next"/><method name="Previous"/><method name="Pause"/><method name="PlayPause"/>
 <method name="Stop"/><method name="Play"/>
 <method name="Seek"><arg direction="in" name="Offset" type="x"/></method>
 <method name="SetPosition"><arg direction="in" name="TrackId" type="o"/><arg direction="in" name="Position" type="x"/></method>
 <method name="OpenUri"><arg direction="in" name="Uri" type="s"/></method>
 <signal name="Seeked"><arg name="Position" type="x"/></signal>
 <property name="PlaybackStatus" type="s" access="read"/><property name="Rate" type="d" access="readwrite"/>
 <property name="Metadata" type="a{sv}" access="read"/><property name="Volume" type="d" access="readwrite"/>
 <property name="Position" type="x" access="read"/>
 <property name="MinimumRate" type="d" access="read"/><property name="MaximumRate" type="d" access="read"/>
 <property name="CanGoNext" type="b" access="read"/><property name="CanGoPrevious" type="b" access="read"/>
 <property name="CanPlay" type="b" access="read"/><property name="CanPause" type="b" access="read"/>
 <property name="CanSeek" type="b" access="read"/><property name="CanControl" type="b" access="read"/>
</interface></node>'''
MPRIS_PATH = '/org/mpris/MediaPlayer2'
P_IFACE = 'org.mpris.MediaPlayer2.Player'


class Mpris:
    def __init__(self, app):
        self.app = app
        self.conn = None
        try:
            self.conn = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            node = Gio.DBusNodeInfo.new_for_xml(MPRIS_XML)
            for iface in node.interfaces:
                self.conn.register_object(MPRIS_PATH, iface, self._call, self._get, self._set)
            name = 'org.mpris.MediaPlayer2.grad_obrechennyj' + os.environ.get('AUDIOBOOK_MPRIS_SUFFIX', '')
            Gio.bus_own_name_on_connection(self.conn, name, Gio.BusNameOwnerFlags.NONE, None, None)
        except GLib.Error as e:
            print('MPRIS unavailable:', e.message, file=sys.stderr)
            self.conn = None

    def _metadata(self):
        a = self.app
        c = a.book.chapters[a.cur_chapter]
        return {
            'mpris:trackid': GLib.Variant('o', f'/local/audiobook/chapter{a.cur_chapter}'),
            'mpris:length': GLib.Variant('x', int((c['end'] - c['g']) * 1e6)),
            'xesam:title': GLib.Variant('s', c['title'] + (f' · {c["part"]}' if c['part'] else '')),
            'xesam:album': GLib.Variant('s', a.book.title),
            'xesam:artist': GLib.Variant('as', [a.book.authors]),
            'mpris:artUrl': GLib.Variant('s', GLib.filename_to_uri(a.book.cover)),
        }

    def _props(self, iface):
        a = self.app
        if iface == 'org.mpris.MediaPlayer2':
            return {'CanQuit': GLib.Variant('b', True), 'CanRaise': GLib.Variant('b', True),
                    'HasTrackList': GLib.Variant('b', False), 'Identity': GLib.Variant('s', a.book.title),
                    'DesktopEntry': GLib.Variant('s', WMCLASS),
                    'SupportedUriSchemes': GLib.Variant('as', []), 'SupportedMimeTypes': GLib.Variant('as', [])}
        c = a.book.chapters[a.cur_chapter]
        return {'PlaybackStatus': GLib.Variant('s', 'Playing' if a.engine.playing else 'Paused'),
                'Rate': GLib.Variant('d', a.engine.rate), 'Metadata': GLib.Variant('a{sv}', self._metadata()),
                'Volume': GLib.Variant('d', a.volume_pos()),
                'Position': GLib.Variant('x', int(max(0.0, a.g() - c['g']) * 1e6)),
                'MinimumRate': GLib.Variant('d', SPEEDS[0]), 'MaximumRate': GLib.Variant('d', SPEEDS[-1]),
                'CanGoNext': GLib.Variant('b', True), 'CanGoPrevious': GLib.Variant('b', True),
                'CanPlay': GLib.Variant('b', True), 'CanPause': GLib.Variant('b', True),
                'CanSeek': GLib.Variant('b', True), 'CanControl': GLib.Variant('b', True)}

    def _get(self, conn, sender, path, iface, prop):
        return self._props(iface).get(prop)

    def _set(self, conn, sender, path, iface, prop, value):
        if prop == 'Volume':
            GLib.idle_add(self.app.set_volume_pos, max(0.0, min(1.0, value.unpack())), True)
        elif prop == 'Rate':
            GLib.idle_add(self.app.set_speed, value.unpack())
        return True

    def _call(self, conn, sender, path, iface, method, params, inv):
        a = self.app
        args = params.unpack()
        if method == 'Raise':
            a.win.present()
        elif method == 'Quit':
            GLib.idle_add(a.quit)
        elif method in ('PlayPause',):
            a.toggle()
        elif method == 'Play':
            a.play()
        elif method in ('Pause', 'Stop'):
            a.pause()
        elif method == 'Next':
            a.chapter_jump(1, 'mediakey')
        elif method == 'Previous':
            a.chapter_jump(-1, 'mediakey')
        elif method == 'Seek':
            a.skip(args[0] / 1e6, 'mediakey')
        elif method == 'SetPosition':
            c = a.book.chapters[a.cur_chapter]
            a.seek_to(c['g'] + args[1] / 1e6, 'seek', reason='mediakey')
        inv.return_value(None)

    def changed(self, *names):
        if not self.conn:
            return
        props = self._props(P_IFACE)
        ch = {n: props[n] for n in names}
        try:
            self.conn.emit_signal(None, MPRIS_PATH, 'org.freedesktop.DBus.Properties', 'PropertiesChanged',
                                  GLib.Variant('(sa{sv}as)', (P_IFACE, ch, [])))
        except GLib.Error:
            pass

    def seeked(self):
        if not self.conn:
            return
        c = self.app.book.chapters[self.app.cur_chapter]
        try:
            self.conn.emit_signal(None, MPRIS_PATH, P_IFACE, 'Seeked',
                                  GLib.Variant('(x)', (int(max(0.0, self.app.g() - c['g']) * 1e6),)))
        except GLib.Error:
            pass


CSS = b'''
.book-title { font-size: 17pt; font-weight: bold; }
.chap-title { font-size: 13pt; font-weight: bold; }
.part-title { font-size: 9pt; }
.small { font-size: 9pt; }
.mono { font-feature-settings: "tnum"; }
.part-header { font-size: 9pt; font-weight: bold; padding: 10px 8px 2px 8px; }
.current-row .row-title { font-weight: bold; color: @theme_selected_bg_color; }
.done-row .row-title { opacity: 0.6; }
.thin progress, .thin trough { min-height: 3px; }
.play-button { min-width: 60px; min-height: 60px; border-radius: 30px; padding: 0; }
.ctl-button { min-width: 44px; min-height: 44px; border-radius: 22px; }
'''


# ---------------------------------------------------------------- application
class Player(Gtk.Application):
    def __init__(self):
        flags = Gio.ApplicationFlags.NON_UNIQUE if TEST else Gio.ApplicationFlags.FLAGS_NONE
        super().__init__(application_id=APP_ID, flags=flags)
        self.win = None

    # ---------- lifecycle
    def do_startup(self):
        Gtk.Application.do_startup(self)
        self.book = Book()
        self.state = State(self.book)
        self.engine = Engine(self.book, self.on_play_state, self.on_track_changed, self.on_error, self.on_eos)
        self.cur_chapter = 0
        self.ui_timer = self.save_timer = self.boundary_timer = self.sleep_timer = self.fade_timer = 0
        self.scale_debounce = 0
        self.ui_active = True
        self.play_started = None          # (g, wallclock) of the current listening stretch
        self.paused_since = time.time()
        self.inhibit_cookie = 0
        self.sleep_mode = 0
        self.sleep_deadline = None
        self.sleep_left = None
        self.note_timer = 0
        self.fade_base = None
        self.dragging = False
        self.status_note = ''
        self.list_dirty = {'chapters': True, 'history': True, 'bookmarks': True, 'files': True}
        self.mpris = Mpris(self)
        self._sleep_lock_fd = None
        self._setup_logind()
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, self._sigterm)
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, self._sigterm)
        if TEST:
            GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGUSR2, self._test_cmds)

    def do_activate(self):
        if self.win:
            self.win.present()
            return
        self.build_ui()
        d = self.state.d
        self.set_volume_pos(round((d.get('volume', 1) or 0) ** (1 / 3), 2), save=False)
        self.set_speed(d.get('speed', 1), record=False)
        saved = self.state.g
        start = self.book.sentence_start(saved) if saved > 2 else saved
        if saved > 2:
            self.state.event('resume', **{'from': saved, 'to': start})
            self.status_note = (f'Resumed at the start of the sentence, {saved - start:.0f} s before where you stopped '
                                f'({self.book.label(saved)}). Press Space or ▶.')
        n, t = self.book.locate(start)
        self.engine.load(n, t, False)
        self.state.set_pos(start)
        self.cur_chapter = self.book.chapter_at(start)
        self.refresh(full=True)

    def do_shutdown(self):
        self.finish_listening()
        try:
            self.state.set_pos(self.g())
            self.state.save(durable=True)
        except Exception as e:  # noqa: BLE001
            print('save failed:', e, file=sys.stderr)
        self.engine.stop()
        Gtk.Application.do_shutdown(self)

    def _sigterm(self):
        self.quit()
        return GLib.SOURCE_REMOVE

    # ---------- suspend: pause + save before sleep
    def _setup_logind(self):
        try:
            self.sysbus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
            self.sysbus.signal_subscribe('org.freedesktop.login1', 'org.freedesktop.login1.Manager', 'PrepareForSleep',
                                         '/org/freedesktop/login1', None, Gio.DBusSignalFlags.NONE, self._prepare_sleep)
            self._take_sleep_lock()
        except GLib.Error:
            self.sysbus = None

    def _take_sleep_lock(self):
        if self._sleep_lock_fd is not None or not self.sysbus:
            return
        try:
            res, fdl = self.sysbus.call_with_unix_fd_list_sync(
                'org.freedesktop.login1', '/org/freedesktop/login1', 'org.freedesktop.login1.Manager', 'Inhibit',
                GLib.Variant('(ssss)', ('sleep', self.book.title, 'Save the listening position', 'delay')),
                GLib.VariantType.new('(h)'), Gio.DBusCallFlags.NONE, -1, None, None)
            self._sleep_lock_fd = fdl.get(res.unpack()[0])
        except GLib.Error:
            self._sleep_lock_fd = None

    def _prepare_sleep(self, conn, sender, path, iface, signal_name, params):
        going = params.unpack()[0]
        if going:
            if self.engine.playing:
                self.pause(reason='suspend')
            self.state.set_pos(self.g())
            self.state.save(durable=True)
            if self._sleep_lock_fd is not None:
                os.close(self._sleep_lock_fd)
                self._sleep_lock_fd = None
        else:
            self._take_sleep_lock()

    # ---------- position helpers
    def g(self):
        if not self.engine.track:
            return self.state.g
        return self.book.starts[self.engine.track - 1] + self.engine.position()

    def volume_pos(self):
        return self.vol_scale.get_value() / 100 if self.win else 1.0

    # ---------- transport
    def toggle(self):
        if self.engine.playing:
            self.pause()
        else:
            self.play()

    def play(self):
        if self.engine.playing:
            return
        idle = time.time() - self.paused_since if self.paused_since else 0
        if idle > LONG_PAUSE:                  # coming back after a while: restart the sentence
            g0 = self.g()
            g1 = self.book.sentence_start(g0)
            if g0 - g1 > 0.3:
                self.state.event('resume', **{'from': g0, 'to': g1})
                self._seek_engine(g1)
        self.engine.play()

    def pause(self, reason='pause'):
        if not self.engine.playing and not self.engine.pending:
            return
        if reason != 'pause':
            self.finish_listening()
            self.state.event(reason, at_pos=self.g())
        self.engine.pause()

    def _seek_engine(self, g):
        n, t = self.book.locate(g)
        if n == self.engine.track:
            self.engine.seek(t)
        else:
            self.engine.load(n, t, self.engine.playing or bool(self.engine.pending and self.engine.pending[1]))
        self.state.set_pos(g)

    def seek_to(self, g, kind, reason=None, record=True):
        g = max(0.0, min(self.book.total - 0.5, g))
        g0 = self.g()
        was_playing = self.engine.playing
        if was_playing:                    # close the listening stretch before logging the jump
            self.finish_listening()
        if record and abs(g - g0) > 0.5:
            kw = {'from': g0, 'to': g}
            if reason:
                kw['reason'] = reason
            self.state.event(kind, **kw)
            self.list_dirty['history'] = True
        if was_playing:
            self.play_started = (g, time.time())
        self._seek_engine(g)
        self.cur_chapter = self.book.chapter_at(g)
        self.mpris.seeked()
        self.arm_boundary_timer()
        self.save_soon()
        self.refresh(full=True)

    def skip(self, d, reason=None):
        self.seek_to(self.g() + d, 'seek', reason=reason or (f'{d:+.0f} s'))

    def chapter_jump(self, d, reason=None):
        g = self.g()
        k = self.book.chapter_at(g)
        if d < 0:
            k = k if (g - self.book.chapters[k]['g'] > 4 or k == 0) else k - 1
        else:
            k = k + 1
            if k >= len(self.book.chapters):
                return
        self.seek_to(self.book.chapters[k]['g'], 'chapter', reason=reason)

    def repeat_sentence(self):
        g = self.g()
        s = self.book.sentence_start(g, lead=0.4)
        if g - s < 1.6:                      # just started this one: go to the previous sentence
            s = self.book.sentence_start(s, lead=0.4)
        self.seek_to(s, 'seek', reason='sentence back')

    def next_sentence(self):
        self.seek_to(self.book.next_sentence(self.g()), 'seek', reason='sentence forward')

    def undo_jump(self):
        for ev in reversed(self.state.d['events']):
            if ev['kind'] in ('seek', 'chapter', 'bookmark', 'file', 'history', 'book') and 'from' in ev:
                self.seek_to(ev['from'], 'history', reason='undo')
                return

    def set_speed(self, r, record=True):
        r = min(SPEEDS, key=lambda s: abs(s - float(r)))
        self.state.d['speed'] = r
        self.engine.set_rate(r)
        if self.win:
            self.speed_combo.handler_block(self.speed_handler)
            self.speed_combo.set_active(SPEEDS.index(r))
            self.speed_combo.handler_unblock(self.speed_handler)
        self.mpris.changed('Rate')
        self.arm_boundary_timer()
        if record:
            self.save_soon()
        self.refresh()
        return False

    def set_volume_pos(self, p, save=True):
        p = max(0.0, min(1.0, p))
        amp = p ** 3                           # perceptual curve, like system mixers
        self.engine.pipe.set_property('volume', amp)
        self.engine.pipe.set_property('mute', False)
        self.state.d['volume'] = round(amp, 4)
        if self.win:
            self.vol_scale.handler_block(self.vol_handler)
            self.vol_scale.set_value(round(p * 100))
            self.vol_scale.handler_unblock(self.vol_handler)
            self._update_mute_icon()
        self.mpris.changed('Volume')
        if save:
            self.save_soon()
        return False

    def toggle_mute(self):
        m = not self.engine.pipe.get_property('mute')
        self.engine.pipe.set_property('mute', m)
        self._update_mute_icon()
        self.flash('Muted' if m else 'Unmuted')

    def _update_mute_icon(self):
        p = self.vol_scale.get_value()
        muted = self.engine.pipe.get_property('mute') or p == 0
        name = 'audio-volume-muted-symbolic' if muted else ('audio-volume-low-symbolic' if p < 35 else
                                                            'audio-volume-medium-symbolic' if p < 70 else 'audio-volume-high-symbolic')
        self.mute_btn.set_image(Gtk.Image.new_from_icon_name(name, Gtk.IconSize.BUTTON))

    # ---------- engine callbacks
    def on_play_state(self, playing):
        if playing:
            if self.status_note.startswith('Resumed at') and not self.note_timer:
                self.status_note = ''
            self.paused_since = None
            self.play_started = (self.g(), time.time())
            if not self.save_timer:
                self.save_timer = GLib.timeout_add_seconds(SAVE_EVERY, self._periodic_save)
            if not self.inhibit_cookie and self.win and not TEST:
                self.inhibit_cookie = self.inhibit(self.win, Gtk.ApplicationInhibitFlags.SUSPEND, 'Playing audiobook')
            if self.sleep_mode and self.sleep_deadline is None:
                self._arm_sleep()
        else:
            self.paused_since = time.time()
            self.finish_listening()
            for attr in ('save_timer', 'sleep_timer', 'fade_timer'):
                if getattr(self, attr):
                    GLib.source_remove(getattr(self, attr))
                    setattr(self, attr, 0)
            if self.sleep_deadline is not None:      # sleep timer pauses with playback
                self.sleep_left = self.sleep_deadline - time.monotonic()
                self.sleep_deadline = None
            if self.fade_base is not None:
                self.engine.pipe.set_property('volume', self.fade_base)
                self.fade_base = None
            if self.inhibit_cookie:
                self.uninhibit(self.inhibit_cookie)
                self.inhibit_cookie = 0
            self.state.set_pos(self.g())
            self.state.save(durable=True)
        self.arm_boundary_timer()
        self.update_ui_timer()
        self.mpris.changed('PlaybackStatus')
        self.refresh(full=True)

    def on_track_changed(self, n):
        self.state.set_pos(self.g())
        self.cur_chapter = self.book.chapter_at(self.g())
        self.save_soon()
        self.arm_boundary_timer()
        self.refresh(full=True)

    def on_eos(self):
        self.finish_listening()
        self.state.event('end', at_pos=self.book.total)
        self.engine.pause()
        self.flash('The end of the book.')

    def on_error(self, text):
        self.flash('Playback error: ' + text)
        print('GStreamer error:', text, file=sys.stderr)

    def finish_listening(self):
        if self.play_started:
            g0, t0 = self.play_started
            g1 = self.g()
            self.play_started = None
            dt = time.time() - t0
            self.state.d['listenedSeconds'] = self.state.d.get('listenedSeconds', 0) + dt
            if dt >= 3 and g1 > g0:
                self.state.event('listen', **{'from': g0, 'to': g1, 'start': int(t0 * 1000)})
                self.list_dirty['history'] = True

    # ---------- timers (all one-shot or 1 Hz-only-when-visible)
    def _periodic_save(self):
        self.state.set_pos(self.g())
        self.state.save(durable=False)
        return True

    def save_soon(self):
        GLib.timeout_add(800, self._save_once)

    def _save_once(self):
        self.state.set_pos(self.g())
        self.state.save(durable=not self.engine.playing)
        return False

    def arm_boundary_timer(self):
        if self.boundary_timer:
            GLib.source_remove(self.boundary_timer)
            self.boundary_timer = 0
        if not self.engine.playing:
            return
        g = self.g()
        c = self.book.chapters[self.book.chapter_at(g)]
        left = (c['end'] - g) / (self.engine.rate or 1)
        self.boundary_timer = GLib.timeout_add_seconds(max(1, int(left) + 1), self._on_boundary)

    def _on_boundary(self):
        self.boundary_timer = 0
        k = self.book.chapter_at(self.g())
        if k != self.cur_chapter:
            self.cur_chapter = k
            self.mpris.changed('Metadata')
            self.list_dirty['chapters'] = True
            self.refresh(full=True)
        self.arm_boundary_timer()
        return False

    def update_ui_timer(self):
        want = self.ui_active and self.engine.playing
        if want and not self.ui_timer:
            self.ui_timer = GLib.timeout_add_seconds(1, self._tick)
        elif not want and self.ui_timer:
            GLib.source_remove(self.ui_timer)
            self.ui_timer = 0

    def _tick(self):
        self.refresh()
        return True

    # ---------- sleep timer
    def set_sleep(self, minutes):
        self.sleep_mode = minutes
        self.sleep_left = None
        self.sleep_deadline = None
        if self.sleep_timer:
            GLib.source_remove(self.sleep_timer)
            self.sleep_timer = 0
        if minutes and self.engine.playing:
            self._arm_sleep()
        self.refresh()

    def _arm_sleep(self):
        if self.sleep_mode == -1:
            g = self.g()
            c = self.book.chapters[self.book.chapter_at(g)]
            left = (c['end'] - g) / (self.engine.rate or 1)
        else:
            left = self.sleep_left if self.sleep_left else self.sleep_mode * 60
        self.sleep_deadline = time.monotonic() + left
        self.sleep_timer = GLib.timeout_add_seconds(max(1, int(left - 10)), self._sleep_fade)

    def _sleep_fade(self):
        self.sleep_timer = 0
        self.fade_base = self.engine.pipe.get_property('volume')
        self.fade_timer = GLib.timeout_add(500, self._fade_step)
        return False

    def _fade_step(self):
        left = self.sleep_deadline - time.monotonic() if self.sleep_deadline else 0
        if left > 0:
            self.engine.pipe.set_property('volume', self.fade_base * max(0.0, left / 10))
            return True
        self.fade_timer = 0
        g = self.g()
        self.sleep_mode, self.sleep_left = 0, None
        self.pause(reason='sleep')
        if self.fade_base is not None:
            self.engine.pipe.set_property('volume', self.fade_base)
            self.fade_base = None
        self.sleep_combo.set_active(0)
        self.flash(f'Sleep timer: paused at {self.book.label(g)}. It is in History.')
        return False

    # ---------- UI
    def build_ui(self):
        prov = Gtk.CssProvider()
        try:
            prov.load_from_data(CSS)
        except GLib.Error as e:
            print('CSS:', e.message, file=sys.stderr)
        Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), prov, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        if os.environ.get('AUDIOBOOK_OFFSCREEN'):
            win = Gtk.OffscreenWindow()
            win.set_size_request(560, 860)
            self.hold()
        else:
            win = Gtk.ApplicationWindow(application=self, title=self.book.title)
            win.set_default_size(560, 860)
            try:
                win.set_icon_from_file(os.path.join(HERE, 'icon.png'))
            except GLib.Error:
                pass
            win.connect('window-state-event', self._on_window_state)
            win.connect('map-event', lambda *a: self._set_active(True))
            win.connect('unmap-event', lambda *a: self._set_active(False))
        win.connect('key-press-event', self.on_key)
        self.win = win

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin=14)
        win.add(outer)

        # header: cover + titles
        head = Gtk.Box(spacing=14)
        try:
            pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(self.book.cover, 84, 120, True)
            head.pack_start(Gtk.Image.new_from_pixbuf(pb), False, False, 0)
        except GLib.Error:
            pass
        tbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, valign=Gtk.Align.CENTER)
        lt = Gtk.Label(label=self.book.title, xalign=0)
        lt.get_style_context().add_class('book-title')
        lb = Gtk.Label(label=f'{self.book.authors} · читает {self.book.reader}', xalign=0, wrap=True)
        lb.get_style_context().add_class('dim-label')
        self.part_label = Gtk.Label(xalign=0, margin_top=10)
        self.part_label.get_style_context().add_class('part-title')
        self.part_label.get_style_context().add_class('dim-label')
        self.chap_label = Gtk.Label(xalign=0)
        self.chap_label.get_style_context().add_class('chap-title')
        for w in (lt, lb, self.part_label, self.chap_label):
            tbox.pack_start(w, False, False, 0)
        head.pack_start(tbox, True, True, 0)
        outer.pack_start(head, False, False, 0)

        # chapter position
        self.scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 1000, 1)
        self.scale.set_draw_value(False)
        self.scale.connect('change-value', self._on_scale_change)
        self.scale.connect('button-press-event', lambda *a: setattr(self, 'dragging', True) or False)
        self.scale.connect('button-release-event', self._on_scale_release)
        outer.pack_start(self.scale, False, False, 0)
        tl = Gtk.Box()
        self.el_label = Gtk.Label(xalign=0)
        self.left_label = Gtk.Label(xalign=1)
        for w in (self.el_label, self.left_label):
            w.get_style_context().add_class('small')
            w.get_style_context().add_class('mono')
        tl.pack_start(self.el_label, True, True, 0)
        tl.pack_end(self.left_label, True, True, 0)
        outer.pack_start(tl, False, False, 0)

        # transport
        ctl = Gtk.Box(spacing=8, halign=Gtk.Align.CENTER, margin_top=4)

        def btn(icon, tip, cb, size=Gtk.IconSize.LARGE_TOOLBAR, cls='ctl-button'):
            b = Gtk.Button(tooltip_text=tip, relief=Gtk.ReliefStyle.NONE)
            b.set_image(Gtk.Image.new_from_icon_name(icon, size))
            b.set_can_focus(False)
            b.get_style_context().add_class(cls)
            b.connect('clicked', lambda *_: cb())
            return b
        ctl.pack_start(btn('media-skip-backward-symbolic', 'Previous chapter (Shift+←)', lambda: self.chapter_jump(-1)), False, False, 0)
        ctl.pack_start(btn('media-seek-backward-symbolic', 'Back 15 s (←)', lambda: self.skip(-15)), False, False, 0)
        self.play_btn = btn('media-playback-start-symbolic', 'Play / pause (Space)', self.toggle, Gtk.IconSize.DND, 'play-button')
        self.play_btn.set_relief(Gtk.ReliefStyle.NORMAL)
        self.play_btn.get_style_context().add_class('suggested-action')
        ctl.pack_start(self.play_btn, False, False, 6)
        ctl.pack_start(btn('media-seek-forward-symbolic', 'Forward 30 s (→)', lambda: self.skip(30)), False, False, 0)
        ctl.pack_start(btn('media-skip-forward-symbolic', 'Next chapter (Shift+→)', lambda: self.chapter_jump(1)), False, False, 0)
        outer.pack_start(ctl, False, False, 0)

        # second row: sentence, speed, sleep, bookmark, volume
        row = Gtk.Box(spacing=6, halign=Gtk.Align.CENTER)
        rs = Gtk.Button(label='↶ Sentence', tooltip_text='Hear the last sentence again (R). Ctrl+Z: back to before the last jump')
        rs.set_can_focus(False)
        rs.connect('clicked', lambda *_: self.repeat_sentence())
        row.pack_start(rs, False, False, 0)
        self.speed_combo = Gtk.ComboBoxText(tooltip_text='Speed ( [ and ] )')
        for s in SPEEDS:
            self.speed_combo.append_text(f'{s:g}×')
        self.speed_combo.set_can_focus(False)
        self.speed_handler = self.speed_combo.connect('changed', lambda c: self.set_speed(SPEEDS[c.get_active()]))
        row.pack_start(self.speed_combo, False, False, 0)
        self.sleep_combo = Gtk.ComboBoxText(tooltip_text='Sleep timer')
        for name, _ in SLEEPS:
            self.sleep_combo.append_text(name)
        self.sleep_combo.set_active(0)
        self.sleep_combo.set_can_focus(False)
        self.sleep_combo.connect('changed', lambda c: self.set_sleep(SLEEPS[c.get_active()][1]))
        row.pack_start(self.sleep_combo, False, False, 0)
        bm = Gtk.Button(tooltip_text='Add bookmark (B)')
        bm.set_image(Gtk.Image.new_from_icon_name('bookmark-new-symbolic', Gtk.IconSize.BUTTON))
        bm.set_can_focus(False)
        bm.connect('clicked', lambda *_: self.add_bookmark())
        row.pack_start(bm, False, False, 0)
        outer.pack_start(row, False, False, 0)

        vrow = Gtk.Box(spacing=4, halign=Gtk.Align.CENTER)
        self.mute_btn = Gtk.Button(relief=Gtk.ReliefStyle.NONE, tooltip_text='Mute (M)')
        self.mute_btn.set_can_focus(False)
        self.mute_btn.connect('clicked', lambda *_: self.toggle_mute())
        self.vol_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
        self.vol_scale.set_size_request(220, -1)
        self.vol_scale.set_value_pos(Gtk.PositionType.RIGHT)
        self.vol_scale.set_digits(0)
        self.vol_scale.set_can_focus(False)
        self.vol_scale.set_tooltip_text('Volume (↑ / ↓). Only this player sets the loudness.')
        self.vol_handler = self.vol_scale.connect('value-changed', lambda s: self.set_volume_pos(s.get_value() / 100))
        vrow.pack_start(self.mute_btn, False, False, 0)
        vrow.pack_start(self.vol_scale, False, False, 0)
        outer.pack_start(vrow, False, False, 0)

        # whole book progress (click to jump)
        ev = Gtk.EventBox()
        self.book_bar = Gtk.ProgressBar(show_text=False, tooltip_text='Whole book — click to jump')
        ev.add(self.book_bar)
        ev.connect('button-press-event', self._on_bookbar_click)
        outer.pack_start(ev, False, False, 2)
        bl = Gtk.Box()
        self.bk_pos = Gtk.Label(xalign=0)
        self.bk_pct = Gtk.Label()
        self.bk_left = Gtk.Label(xalign=1)
        for w in (self.bk_pos, self.bk_pct, self.bk_left):
            w.get_style_context().add_class('small')
            w.get_style_context().add_class('dim-label')
            bl.pack_start(w, True, True, 0)
        outer.pack_start(bl, False, False, 0)

        # lists
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.NONE)
        self.lists = {}
        for name, title in (('chapters', 'Chapters'), ('history', 'History'), ('bookmarks', 'Bookmarks'), ('files', 'Files')):
            lb_ = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
            lb_.set_activate_on_single_click(True)
            lb_.connect('row-activated', self._on_row)
            sw = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
            sw.add(lb_)
            self.stack.add_titled(sw, name, title)
            self.lists[name] = lb_
        sws = Gtk.StackSwitcher(stack=self.stack, halign=Gtk.Align.CENTER, margin_top=6)
        self.stack.connect('notify::visible-child-name', lambda *a: self.refresh_list())
        outer.pack_start(sws, False, False, 0)
        frame = Gtk.Frame()
        frame.add(self.stack)
        outer.pack_start(frame, True, True, 0)

        self.status = Gtk.Label(xalign=0, wrap=True)
        self.status.get_style_context().add_class('small')
        self.status.get_style_context().add_class('dim-label')
        outer.pack_start(self.status, False, False, 0)
        keys = Gtk.Label(xalign=0, wrap=True, label='Space play/pause · ←/→ −15/+30 s · Shift+←/→ chapter · R repeat sentence · '
                                                    'Ctrl+Z undo jump · ↑/↓ volume · [ ] speed · B bookmark · M mute')
        keys.get_style_context().add_class('small')
        keys.get_style_context().add_class('dim-label')
        outer.pack_start(keys, False, False, 0)
        win.show_all()

    def _set_active(self, active):
        self.ui_active = active
        self.update_ui_timer()
        if active:
            self.refresh(full=True)
        return False

    def _on_window_state(self, w, ev):
        self._set_active(not (ev.new_window_state & Gdk.WindowState.ICONIFIED))
        return False

    def _on_scale_change(self, scale, scroll, value):
        c = self.book.chapters[self.cur_chapter]
        v = max(0, min(1000, value))
        self.el_label.set_text(fmt(v / 1000 * (c['end'] - c['g'])))
        if self.scale_debounce:
            GLib.source_remove(self.scale_debounce)
        self.scale_debounce = GLib.timeout_add(350 if not self.dragging else 60000, self._scale_commit)
        return False

    def _on_scale_release(self, *a):
        self.dragging = False
        if self.scale_debounce:
            GLib.source_remove(self.scale_debounce)
            self._scale_commit()
        return False

    def _scale_commit(self):
        self.scale_debounce = 0
        c = self.book.chapters[self.cur_chapter]
        self.seek_to(c['g'] + self.scale.get_value() / 1000 * (c['end'] - c['g']), 'seek', reason='slider')
        return False

    def _on_bookbar_click(self, w, ev):
        a = w.get_allocation()
        self.seek_to(ev.x / max(1, a.width) * self.book.total, 'book')
        return True

    def flash(self, text, sticky=False):
        self.status_note = text
        if self.note_timer:
            GLib.source_remove(self.note_timer)
            self.note_timer = 0
        if not sticky:
            self.note_timer = GLib.timeout_add_seconds(6, self._clear_note)
        self.refresh()

    def _clear_note(self):
        self.note_timer = 0
        self.status_note = ''
        self.refresh()
        return False

    def refresh(self, full=False):
        if not self.win:
            return
        g = self.g()
        b = self.book
        k = b.chapter_at(g)
        if k != self.cur_chapter:
            self.cur_chapter = k
            self.mpris.changed('Metadata')
            full = True
        c = b.chapters[k]
        rate = self.engine.rate or 1
        cl, ce = c['end'] - c['g'], g - c['g']
        if full:
            self.part_label.set_text((c['part'] or b.title).upper())
            self.chap_label.set_text(c['title'])
            icon = 'media-playback-pause-symbolic' if self.engine.playing else 'media-playback-start-symbolic'
            self.play_btn.set_image(Gtk.Image.new_from_icon_name(icon, Gtk.IconSize.DND))
            self.win.set_title(f'{"▶ " if self.engine.playing else ""}{c["title"]} — {b.title}') if hasattr(self.win, 'set_title') else None
            self.list_dirty['chapters'] = self.list_dirty['files'] = True
            self.refresh_list()
        if not self.dragging and not self.scale_debounce:
            self.scale.set_value(ce / cl * 1000 if cl else 0)
            self.el_label.set_text(fmt(ce))
        self.left_label.set_text('−' + fmt((cl - ce) / rate))
        self.book_bar.set_fraction(min(1.0, g / b.total))
        self.bk_pos.set_text(f'{fmt(g)} / {fmt(b.total)}')
        self.bk_pct.set_text(f'{g / b.total * 100:.1f}%')
        self.bk_left.set_text(f'{fmt_long((b.total - g) / rate)} left' + (f' at {rate:g}×' if rate != 1 else ''))
        bits = []
        if self.state.saved_at:
            bits.append(f'Position saved {time.strftime("%H:%M:%S", time.localtime(self.state.saved_at))}')
        n, t = b.locate(g)
        bits.append(f'file {n:02d}.mp3 · {fmt(t)}')
        if self.sleep_deadline is not None:
            bits.append(f'☾ sleep in {fmt(self.sleep_deadline - time.monotonic())}')
        elif self.sleep_mode:
            bits.append('☾ sleep timer set')
        text = ' · '.join(bits)
        if self.status_note:
            text = self.status_note + '\n' + text
        self.status.set_text(text)
        if self.stack.get_visible_child_name() == 'chapters' and not full:
            self._update_current_chapter_bar(g)

    # ---------- lists
    def refresh_list(self):
        name = self.stack.get_visible_child_name()
        if not name or not self.list_dirty.get(name):
            return
        self.list_dirty[name] = False
        lb = self.lists[name]
        for ch in lb.get_children():
            lb.remove(ch)
        getattr(self, f'_fill_{name}')(lb)
        lb.show_all()
        if name in ('chapters', 'files'):
            GLib.idle_add(self._scroll_to_current, lb)

    def _scroll_to_current(self, lb):
        for row in lb.get_children():
            if getattr(row, 'is_current', False):
                adj = lb.get_parent().get_parent().get_vadjustment() if isinstance(lb.get_parent(), Gtk.Viewport) else None
                a = row.get_allocation()
                if adj and (a.y < adj.get_value() or a.y + a.height > adj.get_value() + adj.get_page_size()):
                    adj.set_value(max(0, a.y - 80))
                break
        return False

    def _row(self, title, right='', sub='', target=None, cls=None, bar=None, buttons=()):
        row = Gtk.ListBoxRow()
        row.target = target
        grid = Gtk.Grid(column_spacing=10, row_spacing=1, margin_start=10, margin_end=10, margin_top=6, margin_bottom=6)
        lt = Gtk.Label(label=title, xalign=0, hexpand=True, wrap=True)
        lt.get_style_context().add_class('row-title')
        grid.attach(lt, 0, 0, 1, 1)
        col = 1
        if right:
            lr = Gtk.Label(label=right, xalign=1)
            lr.get_style_context().add_class('dim-label')
            lr.get_style_context().add_class('mono')
            grid.attach(lr, col, 0, 1, 1)
            col += 1
        for (label, tip, cb) in buttons:
            bb = Gtk.Button(label=label, tooltip_text=tip, valign=Gtk.Align.CENTER)
            bb.set_can_focus(False)
            bb.connect('clicked', lambda _b, f=cb: f())
            grid.attach(bb, col, 0, 1, 2)
            col += 1
        if sub:
            ls = Gtk.Label(label=sub, xalign=0, wrap=True)
            ls.get_style_context().add_class('small')
            ls.get_style_context().add_class('dim-label')
            grid.attach(ls, 0, 1, 1, 1)
        if bar is not None:
            pb = Gtk.ProgressBar(fraction=bar)
            pb.get_style_context().add_class('thin')
            grid.attach(pb, 0, 2, max(1, col), 1)
            row.bar = pb
        row.add(grid)
        if cls:
            row.get_style_context().add_class(cls)
        return row

    def _header(self, text):
        row = Gtk.ListBoxRow(activatable=False, selectable=False)
        lab = Gtk.Label(label=text.upper(), xalign=0)
        lab.get_style_context().add_class('part-header')
        lab.get_style_context().add_class('dim-label')
        row.add(lab)
        row.target = None
        return row

    def _fill_chapters(self, lb):
        g = self.g()
        k = self.book.chapter_at(g)
        part = None
        for i, c in enumerate(self.book.chapters):
            if c['part'] != part:
                part = c['part']
                if part:
                    lb.add(self._header(part))
            frac = 1.0 if i < k else (0.0 if i > k else (g - c['g']) / (c['end'] - c['g']))
            row = self._row(c['title'], fmt(c['end'] - c['g']), f'starts {fmt(c["g"])} · file {c["track"]:02d}',
                            target=('chapter', c['g']), cls='current-row' if i == k else ('done-row' if i < k else None),
                            bar=frac)
            row.is_current = i == k
            row.chapter_index = i
            lb.add(row)

    def _update_current_chapter_bar(self, g):
        for row in self.lists['chapters'].get_children():
            if getattr(row, 'is_current', False) and hasattr(row, 'bar'):
                c = self.book.chapters[row.chapter_index]
                row.bar.set_fraction(max(0.0, min(1.0, (g - c['g']) / (c['end'] - c['g']))))

    def _fill_files(self, lb):
        n0, _ = self.book.locate(self.g())
        for i, t in enumerate(self.book.tracks, 1):
            row = self._row(f'{i:02d}.mp3', fmt(t['dur']), f'{t["label"]} · starts {fmt(self.book.starts[i - 1])} · '
                            f'{self.book.label(self.book.starts[i - 1])}', target=('file', self.book.starts[i - 1]),
                            cls='current-row' if i == n0 else ('done-row' if i < n0 else None))
            row.is_current = i == n0
            lb.add(row)

    def _fill_bookmarks(self, lb):
        B = sorted(self.state.d['bookmarks'], key=lambda m: m['g'])
        if not B:
            lb.add(self._row('No bookmarks yet — press B or the bookmark button.'))
        for m in B:
            when = time.strftime('%d.%m %H:%M', time.localtime(m.get('at', 0) / 1000))
            lb.add(self._row(m.get('note') or self.book.label(m['g'], short=False), fmt(m['g']),
                             f'{self.book.label(m["g"], short=False)} · added {when}', target=('bookmark', m['g']),
                             buttons=[('✕', 'Delete bookmark', lambda m=m: self.delete_bookmark(m))]))

    def _fill_history(self, lb):
        E = list(reversed(self.state.d['events']))
        if not E:
            lb.add(self._row('Nothing yet. Every rewind, jump and listening stretch will be listed here.'))
        today = time.strftime('%Y%m%d')
        lab = self.book.label
        for ev in E[:250]:
            t = ev['at'] / 1000
            day = time.strftime('%Y%m%d', time.localtime(t))
            when = time.strftime('%H:%M' if day == today else '%d.%m %H:%M', time.localtime(t))
            k = ev['kind']
            fr, to = ev.get('from'), ev.get('to')
            btns = []
            if k == 'listen':
                st = time.strftime('%H:%M', time.localtime(ev.get('start', ev['at']) / 1000))
                title = f'▶ Listened {lab(fr)} → {lab(to)}'
                sub = f'{st}–{when} · {fmt(to - fr)} of the book'
                btns = [('Go to end', 'Continue from where this stretch ended', lambda g=to: self.seek_to(g, 'history'))]
            elif k == 'resume':
                title = f'⟲ Resumed at sentence start {lab(to)}'
                sub = f'{when} · stopped at {lab(fr)} ({fr - to:.0f} s later)'
            elif k in ('sleep', 'suspend', 'end'):
                p = ev.get('at_pos', 0)
                title = {'sleep': '☾ Sleep timer paused', 'suspend': '⏾ Paused for suspend', 'end': '■ End of book'}[k] + f' at {lab(p)}'
                sub = when
                btns = [('Go there', 'Jump to this point', lambda g=p: self.seek_to(g, 'history'))]
            else:
                d = to - fr
                reason = ev.get('reason') or k
                cnt = f' ×{ev["count"]}' if ev.get('count', 1) > 1 else ''
                arrow = '⏪' if d < 0 else '⏩'
                if k == 'chapter':
                    title = f'{arrow} Chapter jump {lab(fr)} → {lab(to)}'
                elif ev.get('reason') == 'undo':
                    title = f'↶ Undo jump {lab(fr)} → {lab(to)}'
                elif k in ('bookmark', 'file', 'book', 'history'):
                    title = f'{arrow} {k.capitalize()} jump {lab(fr)} → {lab(to)}'
                else:
                    title = f'{arrow} {"−" if d < 0 else "+"}{fmt(abs(d))} ({reason}{cnt}) {lab(fr)} → {lab(to)}'
                sub = when
                btns = [('Back', f'Return to {lab(fr)}, where you were before this', lambda g=fr: self.seek_to(g, 'history'))]
            lb.add(self._row(title, '', sub, target=None, buttons=btns))

    def _on_row(self, lb, row):
        tgt = getattr(row, 'target', None)
        if not tgt:
            return
        kind, g = tgt
        self.seek_to(g, kind)
        if not self.engine.playing:
            self.play()

    # ---------- bookmarks
    def add_bookmark(self):
        g = self.g()
        note = ''
        if not TEST:
            dlg = Gtk.Dialog(title='Add bookmark', transient_for=self.win, modal=True)
            dlg.add_buttons('Cancel', Gtk.ResponseType.CANCEL, 'Add', Gtk.ResponseType.OK)
            dlg.set_default_response(Gtk.ResponseType.OK)
            entry = Gtk.Entry(activates_default=True, placeholder_text='Note (optional)')
            box = dlg.get_content_area()
            box.set_spacing(6)
            box.set_margin_top(10)
            box.set_margin_start(10)
            box.set_margin_end(10)
            box.add(Gtk.Label(label=self.book.label(g, short=False), xalign=0))
            box.add(entry)
            dlg.show_all()
            resp = dlg.run()
            note = entry.get_text().strip()
            dlg.destroy()
            if resp != Gtk.ResponseType.OK:
                return
        self.state.d['bookmarks'].append({'id': now_ms(), 'g': g, 'note': note, 'at': now_ms()})
        self.list_dirty['bookmarks'] = True
        self.save_soon()
        self.refresh_list()
        self.flash(f'Bookmark added at {self.book.label(g)}')

    def delete_bookmark(self, m):
        if not TEST:
            d = Gtk.MessageDialog(transient_for=self.win, modal=True, message_type=Gtk.MessageType.QUESTION,
                                  buttons=Gtk.ButtonsType.YES_NO, text='Delete this bookmark?')
            d.format_secondary_text(m.get('note') or self.book.label(m['g'], short=False))
            r = d.run()
            d.destroy()
            if r != Gtk.ResponseType.YES:
                return
        self.state.d['bookmarks'] = [x for x in self.state.d['bookmarks'] if x is not m]
        self.list_dirty['bookmarks'] = True
        self.save_soon()
        self.refresh_list()

    # ---------- keyboard
    def on_key(self, w, ev):
        k = Gdk.keyval_name(ev.keyval) or ''
        shift = bool(ev.state & Gdk.ModifierType.SHIFT_MASK)
        ctrl = bool(ev.state & Gdk.ModifierType.CONTROL_MASK)
        if isinstance(self.win.get_focus(), Gtk.Entry):
            return False
        if k in ('space', 'k', 'K'):
            self.toggle()
        elif k == 'Left' and shift:
            self.chapter_jump(-1)
        elif k == 'Right' and shift:
            self.chapter_jump(1)
        elif k in ('Left', 'j', 'J'):
            self.skip(-15)
        elif k in ('Right', 'l', 'L'):
            self.skip(30)
        elif k in ('z', 'Z') and ctrl:
            self.undo_jump()
        elif k in ('r', 'R', 'BackSpace'):
            self.repeat_sentence()
        elif k in ('Up', 'Down'):
            p = self.vol_scale.get_value() / 100 + (0.04 if k == 'Up' else -0.04)
            self.set_volume_pos(round(p, 2))
            self.flash(f'Volume {round(max(0, min(1, p)) * 100)}%')
        elif k in ('bracketleft', 'bracketright'):
            i = SPEEDS.index(self.state.d['speed']) + (1 if k == 'bracketright' else -1)
            self.set_speed(SPEEDS[max(0, min(len(SPEEDS) - 1, i))])
            self.flash(f'Speed {self.state.d["speed"]:g}×')
        elif k in ('b', 'B'):
            self.add_bookmark()
        elif k in ('m', 'M'):
            self.toggle_mute()
        elif k in ('q', 'Q') and ctrl:
            self.quit()
        else:
            return False
        return True

    # ---------- test hook (only with AUDIOBOOK_TEST_CMDS set): SIGUSR2 runs commands from that file
    def _test_cmds(self):
        try:
            lines = open(os.environ['AUDIOBOOK_TEST_CMDS']).read().splitlines()
        except OSError:
            return True
        for line in lines:
            parts = line.split(maxsplit=1)
            if not parts:
                continue
            cmd, arg = parts[0], (parts[1] if len(parts) > 1 else '')
            if cmd == 'key':
                mods, _, name = arg.rpartition('+')
                e = types.SimpleNamespace()
                e.keyval = Gdk.keyval_from_name(name)
                e.state = (Gdk.ModifierType.SHIFT_MASK if 'shift' in mods else 0) | (Gdk.ModifierType.CONTROL_MASK if 'ctrl' in mods else 0)
                self.on_key(self.win, e)
            elif cmd == 'tab':
                self.stack.set_visible_child_name(arg)
            elif cmd == 'chapter':
                self.seek_to(self.book.chapters[int(arg)]['g'], 'chapter')
            elif cmd == 'sleep':
                self.sleep_combo.set_active(int(arg))
            elif cmd == 'shot':
                while Gtk.events_pending():
                    Gtk.main_iteration()
                self.win.get_pixbuf().savev(arg, 'png', [], [])
            elif cmd == 'active':
                self._set_active(arg == '1')
            elif cmd == 'pausedago':
                self.paused_since = time.time() - float(arg)
            elif cmd == 'dump':
                g = self.g()
                json.dump({'g': g, 'label': self.book.label(g, short=False), 'track': self.engine.track,
                           'pos': self.engine.position(), 'playing': self.engine.playing, 'rate': self.engine.rate,
                           'volume': self.engine.pipe.get_property('volume'), 'events': self.state.d['events'][-8:],
                           'status': self.status.get_text(), 'ui_timer': bool(self.ui_timer),
                           'save_timer': bool(self.save_timer), 'boundary_timer': bool(self.boundary_timer)},
                          open(arg, 'w'), ensure_ascii=False, indent=1)
            elif cmd == 'quit':
                self.quit()
        return True


def main():
    # Use the existing GStreamer plugin cache: no rescan at start (a rescan here trips a crashing
    # VA-API video plugin and costs seconds of CPU). Only audio plugins are used.
    cache = os.path.join(GLib.get_user_cache_dir(), 'gstreamer-1.0')
    if os.path.isdir(cache) and any(f.startswith('registry.') for f in os.listdir(cache)):
        os.environ.setdefault('GST_REGISTRY_UPDATE', 'no')
    GLib.set_prgname(WMCLASS)
    GLib.set_application_name('Град обреченный')
    Gdk.set_program_class(WMCLASS)
    Gst.init(None)
    return Player().run(sys.argv[:1])


if __name__ == '__main__':
    sys.exit(main())
