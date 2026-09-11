#!/usr/bin/env python3
"""
Local YouTube-style player v13.

Playback architecture:
  * The complete video downloads continuously with yt-dlp in the background.
  * Playback is built from independent, real fragmented-MP4 chunk files.
  * Chunk sizes are 3s, then 5s, then 10s forever for a new playback path.
  * The browser uses Media Source Extensions to append the individual MP4
    chunks into one continuous timeline.
  * If a seek lands inside an already-completed chunk, no new download is
    started. If it lands inside a chunk currently being produced, playback
    simply buffers until that chunk completes.
  * A seek outside all existing chunks pauses the active chunk worker, keeps
    its partial work, and starts a new 3/5/10-second sequence at the target.
  * When the full download completes, temporary chunks are deleted and the
    browser switches to the complete MP4 for unrestricted seeking.
"""

import contextlib
import http.server
import json
import mimetypes
import os
import re
import shutil
import signal
import socket
import socketserver
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
import webbrowser
from pathlib import Path

HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", 5000))
VIDEOS_DIR = Path(__file__).resolve().parent / "videos"
VIDEOS_DIR.mkdir(exist_ok=True)

YTDLP_COOKIE_FILE = "/etc/secrets/cookies.txt"

CHUNK_SIZES = (3.0, 5.0, 10.0)
CHUNK_SIZE_FOREVER = 10.0
FFMPEG_TIMEOUT = 180
FFPROBE_TIMEOUT = 30
MAX_REQUEST_BODY = 8192
POLL_INTERVAL = 0.15
_ENCODER_CACHE = None

_LOG_LOCK = threading.Lock()
SESSIONS = {}
SESSIONS_LOCK = threading.Lock()


def log(level, msg):
    with _LOG_LOCK:
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {level:<8} {msg}", flush=True)


def redact_url(url):
    try:
        p = urllib.parse.urlsplit(url)
        pairs = urllib.parse.parse_qsl(p.query, keep_blank_values=True)
        secret = {"signature", "sig", "token", "auth", "key", "authorization", "sp"}
        pairs = [(k, "REDACTED" if k.lower() in secret else v) for k, v in pairs]
        return urllib.parse.urlunsplit((p.scheme, p.netloc, p.path, urllib.parse.urlencode(pairs), ""))
    except Exception:
        return "<url>"


def check_yt_dlp():
    try:
        import yt_dlp
        return True, yt_dlp
    except ImportError:
        return False, None


def check_ffmpeg():
    return shutil.which("ffmpeg") is not None


def available_h264_encoder():
    """Prefer a hardware encoder when the installed FFmpeg exposes one.

    AMD AMF is useful on the user's RX 7800 XT because playback encoding is a
    temporary proxy job. If AMF is unavailable, fall back to the very portable
    libx264 ultrafast encoder. The actual chunk/timeline logic is identical.
    """
    global _ENCODER_CACHE
    if _ENCODER_CACHE is not None:
        return _ENCODER_CACHE
    try:
        p = subprocess.run(
            ["ffmpeg", "-hide_banner", "-encoders"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, timeout=10
        )
        text = p.stdout or ""
        if " h264_amf " in text or " h264_amf\n" in text:
            _ENCODER_CACHE = "h264_amf"
        elif " libx264 " in text or " libx264\n" in text:
            _ENCODER_CACHE = "libx264"
        else:
            _ENCODER_CACHE = None
    except Exception:
        _ENCODER_CACHE = "libx264"
    return _ENCODER_CACHE


YTDLP_AVAILABLE, yt_dlp = check_yt_dlp()
FFMPEG_AVAILABLE = check_ffmpeg()
_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{4,32}$")
_YOUTUBE_HOST_RE = re.compile(r"^(www\.|m\.|music\.)?(youtube\.com|youtu\.be)$", re.I)


def sanitize_video_id(v):
    if not v or not _VIDEO_ID_RE.match(v):
        raise ValueError("invalid video id")
    return v


def is_youtube_url(url):
    try:
        p = urllib.parse.urlsplit(url)
        return p.scheme in ("http", "https") and bool(_YOUTUBE_HOST_RE.match(p.netloc))
    except Exception:
        return False


def fmt_size(n):
    if not n:
        return "0 B"
    n = float(n)
    for u in ("B", "KiB", "MiB", "GiB"):
        if n < 1024 or u == "GiB":
            return f"{n:.1f} {u}"
        n /= 1024


def atomic_json(path, data):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def extract_info(url):
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "cookiefile": YTDLP_COOKIE_FILE,
    }

    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)


def is_h264(f):
    return (f.get("vcodec") or "none").lower().startswith("avc1")


def is_aac(f):
    return (f.get("acodec") or "none").lower().startswith("mp4a")


def choose_streams(info):
    """
    Pick a browser-friendly H.264/AAC source pair.

    Audio selection is deliberately language-aware. YouTube/yt-dlp can expose
    multiple dubbed tracks, and simply taking the highest-bitrate AAC stream
    can accidentally select a dub. We prefer English, then an explicitly
    original/default track, then a neutral fallback.
    """
    formats = info.get("formats", [])
    progressive, videos, audios = [], [], []

    def lang_score(f):
        lang = str(f.get("language") or "").lower()
        note = str(f.get("format_note") or "").lower()
        name = str(f.get("language_preference") or "").lower()
        # Strong preference for actual English language metadata.
        if lang in ("en", "en-us", "en-gb", "en-ca", "en-au"):
            score = 1000
        elif lang.startswith("en-"):
            score = 950
        elif lang:
            score = 100
        else:
            score = 300

        # "original/default" is important when YouTube's language metadata is
        # missing or known to be unreliable for dubbed tracks.
        if "original" in note:
            score += 350
        if "default" in note:
            score += 250
        if "original" in name:
            score += 150
        if "dub" in note or "dubbed" in note:
            score -= 400
        if "auto-dub" in note:
            score -= 500
        return score

    for f in formats:
        protocol = (f.get("protocol") or "").lower()
        if protocol not in ("http", "https", "http_dash_segments"):
            continue
        ext = (f.get("ext") or "").lower()
        if ext not in ("mp4", "m4a"):
            continue
        if not f.get("url"):
            continue
        vc = (f.get("vcodec") or "none").lower()
        ac = (f.get("acodec") or "none").lower()
        if vc != "none" and ac != "none" and ext == "mp4":
            progressive.append(f)
        elif vc != "none" and ac == "none" and is_h264(f) and ext == "mp4":
            videos.append(f)
        elif vc == "none" and ac != "none" and is_aac(f) and ext in ("m4a", "mp4"):
            audios.append(f)

    progressive.sort(key=lambda f: (f.get("height") or 0, f.get("tbr") or 0), reverse=True)
    videos.sort(key=lambda f: (f.get("height") or 0, f.get("tbr") or 0), reverse=True)
    # Language first, then audio quality. This prevents a high-bitrate dubbed
    # stream from winning solely because its ABR is higher.
    audios.sort(
        key=lambda f: (
            lang_score(f),
            f.get("abr") or 0,
            f.get("tbr") or 0,
            f.get("filesize") or 0,
        ),
        reverse=True,
    )

    # Prefer an explicit video+audio pair whenever possible. This lets us
    # choose the audio language deliberately instead of inheriting whatever
    # audio track happened to be embedded in a progressive format.
    if videos and audios:
        return (None, videos[0], audios[0])
    return (progressive[0] if progressive else None, None, None)


def header_blob(fmt):
    headers = (fmt or {}).get("http_headers") or {}
    lines = []
    for k, v in headers.items():
        if k.lower() in ("range", "content-length", "host"):
            continue
        lines.append(f"{k}: {v}")
    return "\r\n".join(lines) + ("\r\n" if lines else "")


def ffmpeg_input_args(fmt):
    blob = header_blob(fmt)
    return ["-headers", blob] if blob else []


def stream_url(fmt):
    return (fmt or {}).get("url")


def codec_string(video_fmt, audio_fmt):
    v = (video_fmt or {}).get("vcodec") or "avc1.4d401f"
    a = (audio_fmt or {}).get("acodec") or "mp4a.40.2"
    # yt-dlp normally gives exactly the codec strings accepted by MSE.
    v = v.split(" ", 1)[0]
    a = a.split(" ", 1)[0]
    return f'video/mp4; codecs="{v},{a}"'


class Chunk:
    def __init__(self, index, start, end, path):
        self.index = int(index)
        self.start = float(start)
        self.end = float(end)
        self.path = Path(path)
        self.status = "complete" if self.path.exists() else "pending"
        self.error = None
        self.process = None
        self.lock = threading.RLock()
        self.generation = 0
        self.actual_duration = None

    @property
    def duration(self):
        return max(0.0, self.end - self.start)

    def to_dict(self):
        with self.lock:
            size = self.path.stat().st_size if self.path.exists() else 0
            return {
                "index": self.index,
                "start": self.start,
                "end": self.end,
                "duration": self.duration,
                "actual_duration": self.actual_duration,
                "status": self.status,
                "file": self.path.name if self.path.exists() else None,
                "size": size,
                "error": self.error,
            }


class DownloadSession:
    def __init__(self, video_id, title, duration):
        self.video_id = video_id
        self.title = title
        self.duration = float(duration or 0)
        self.cache_dir = VIDEOS_DIR / video_id
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.original_url = None
        self.status = "loading"
        self.error_message = None

        self.full_total = 0
        self.full_downloaded = 0
        self.full_exact = False
        self.full_rate = 0.0
        self.full_done = False
        self.full_file = None
        self.full_thread = None
        self._rate = []
        self.lock = threading.RLock()

        self.video_fmt = None
        self.audio_fmt = None
        self.progressive_fmt = None
        self.mode = "chunked"
        self.mime = None

        self.chunks = {}
        self.chunk_lock = threading.RLock()
        self.chunk_worker = None
        self.chunk_wake = threading.Event()
        self.chunk_stop = threading.Event()
        self.frontier_start = 0.0
        self.frontier_cursor = 0.0
        self.frontier_step = 0
        self.active_chunk = None
        self.requested_target = 0.0
        self.current_playback_chunk = None

        # One FFmpeg process owns each forward playback sequence.  This is the
        # important v10 change: audio encoding/packet flow is continuous across
        # segment boundaries instead of restarting FFmpeg for every tiny chunk.
        self.sequence_generation = 0
        self.sequence_process = None
        self.sequence_dir = None
        self.sequence_start = 0.0
        self.sequence_index_base = 0
        self.next_chunk_index = 0
        self.sequence_lock = threading.RLock()

    @property
    def final_file(self):
        if self.full_file and self.full_file.exists():
            return self.full_file
        p = self.cache_dir / "full.mp4"
        return p if p.exists() and self.full_done else None

    def record_full(self, n, total=None, exact=False):
        now = time.time()
        with self.lock:
            self.full_downloaded = int(n or 0)
            if total:
                self.full_total = int(total)
            self.full_exact = bool(exact)
            self._rate.append((now, self.full_downloaded))
            cutoff = now - 5
            self._rate = [x for x in self._rate if x[0] >= cutoff]
            if len(self._rate) >= 2:
                a, b = self._rate[0], self._rate[-1]
                self.full_rate = max(0, (b[1] - a[1]) / max(0.5, b[0] - a[0]))

    def progress(self):
        with self.lock:
            if self.full_total:
                return min(100.0, self.full_downloaded / self.full_total * 100)
            return None


def chunk_path(s, index, start, end):
    return s.cache_dir / f"chunk_{index:06d}_{start:.3f}_{end:.3f}.mp4"


def probe_media_details(path):
    try:
        p = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "format=duration,start_time:stream=index,codec_type,start_time,duration",
             "-of", "json", str(path)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=FFPROBE_TIMEOUT,
        )
        if p.returncode != 0:
            return None
        data = json.loads(p.stdout or "{}")
        streams = data.get("streams") or []
        video = next((x for x in streams if x.get("codec_type") == "video"), {})
        audio = next((x for x in streams if x.get("codec_type") == "audio"), {})
        return {
            "streams": len(streams),
            "format_duration": data.get("format", {}).get("duration"),
            "format_start": data.get("format", {}).get("start_time"),
            "video_start": video.get("start_time"),
            "audio_start": audio.get("start_time"),
            "video_duration": video.get("duration"),
            "audio_duration": audio.get("duration"),
        }
    except Exception as e:
        log("DEBUG", f"ffprobe details failed for {path.name}: {e}")
        return None


def probe_media_duration(path):
    try:
        p = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            timeout=FFPROBE_TIMEOUT,
        )
        if p.returncode == 0:
            value = float((p.stdout or "").strip())
            if value > 0:
                return value
    except Exception:
        pass
    return None


def _register_sequence_segments(s, stop_reason=None):
    """Register segments on the SOURCE timeline, never on accumulated encoded duration.

    Earlier versions had two separate timeline problems: cumulative-duration compression and stream-copy keyframe cuts. V13 fixes both: if FFmpeg produced a
    10s requested segment with only 7s of muxed duration, the next segment was
    registered at +7 instead of +10. After many segments that can turn a 60s
    source event into something around 20-30s in the browser. v12 keeps the
    requested 3/5/10 boundaries as the authoritative source timeline and stores
    actual media duration separately for diagnostics.
    """
    with s.sequence_lock:
        seqdir = s.sequence_dir
        base = s.sequence_index_base
        seq_start = s.sequence_start
    if not seqdir or not seqdir.exists():
        return

    files = sorted(seqdir.glob("seg_*.mp4"))
    for path in files:
        try:
            n = int(path.stem.split("_")[-1])
        except Exception:
            continue
        idx = base + n
        with s.chunk_lock:
            if idx in s.chunks and s.chunks[idx].status == "complete":
                continue
        actual = probe_media_duration(path)
        if actual is None:
            continue

        expected_start, expected_end = _expected_segment_bounds(seq_start, n, s.duration)
        actual_probe = probe_media_details(path)
        with s.chunk_lock:
            if idx in s.chunks and s.chunks[idx].status == "complete":
                continue
            # IMPORTANT: source/requested timeline is authoritative.
            # Never accumulate ffprobe durations here. Stream-copy segmentation
            # can produce shorter/longer physical files around keyframes; using
            # those durations as timeline positions progressively compresses or
            # stretches playback (the v10/v11 bug).
            start = expected_start
            end = expected_end
            if end <= start + 0.001:
                continue
            c = Chunk(idx, start, end, path)
            c.actual_duration = actual
            c.status = "complete"
            s.chunks[idx] = c
            s.next_chunk_index = max(s.next_chunk_index, idx + 1)
            if s.current_playback_chunk is None:
                s.current_playback_chunk = idx

        drift_start = start - expected_start
        drift_end = end - expected_end
        log("CHUNK", f"{s.video_id}: segment {idx} READY (SOURCE TIMELINE AUTHORITY)")
        log("TIMELINE", f"{s.video_id}: seg={idx} REQUESTED [{expected_start:.3f},{expected_end:.3f}] "
            f"ACTUAL [{start:.3f},{end:.3f}] duration={actual:.3f}s "
            f"drift_start={drift_start:+.3f}s drift_end={drift_end:+.3f}s")
        if actual_probe:
            log("MEDIA", f"{s.video_id}: seg={idx} streams="
                f"{actual_probe.get('streams')} format_duration={actual_probe.get('format_duration')} "
                f"format_start={actual_probe.get('format_start')} "
                f"video_start={actual_probe.get('video_start')} audio_start={actual_probe.get('audio_start')} "
                f"video_dur={actual_probe.get('video_duration')} audio_dur={actual_probe.get('audio_duration')}")
        expected_duration = max(0.0, expected_end - expected_start)
        duration_error = actual - expected_duration
        if abs(duration_error) > 0.15:
            log("WARNING", f"{s.video_id}: PHYSICAL DURATION MISMATCH seg={idx}: expected={expected_duration:.3f}s actual={actual:.3f}s error={duration_error:+.3f}s")
        if abs(duration_error) > 0.75:
            log("ERROR", f"{s.video_id}: CHUNK MAY BE INVALID seg={idx}: expected {expected_duration:.3f}s but file contains {actual:.3f}s")
        if abs(drift_end) > 0.75:
            log("WARNING", f"{s.video_id}: LARGE TIMELINE DRIFT on segment {idx}: {drift_end:+.3f}s")



def _stop_sequence(s, reason="stop", graceful=False):
    with s.sequence_lock:
        p = s.sequence_process
        s.sequence_process = None
        s.sequence_generation += 1
    if p and p.poll() is None:
        log("CHUNK", f"{s.video_id}: stopping FFmpeg sequence ({reason})")
        if graceful:
            try:
                if p.stdin:
                    p.stdin.write(b"q\n")
                    p.stdin.flush()
            except Exception:
                pass
            try:
                p.wait(timeout=5)
            except Exception:
                pass
        if p.poll() is None:
            try:
                p.terminate()
            except Exception:
                pass
            try:
                p.wait(timeout=2)
            except Exception:
                pass
        if p.poll() is None:
            try:
                p.kill()
            except Exception:
                pass
            try:
                p.wait(timeout=3)
            except Exception:
                pass
    # A stopped sequence may have a final short-but-valid segment. Probe and
    # register it after the process has fully exited, but never reference an
    # undefined stop_reason (the V13 fixed-build crash).
    _register_sequence_segments(s, stop_reason=reason)


def _build_segment_times(relative_duration):
    # 3 seconds, then 5 seconds, then 10 seconds forever.
    cuts = []
    t = 3.0
    if relative_duration <= 3.0:
        return cuts
    cuts.append(t)
    t = 8.0
    if relative_duration <= 8.0:
        return cuts
    cuts.append(t)
    while t + 10.0 < relative_duration - 0.02:
        t += 10.0
        cuts.append(t)
    return cuts


def _expected_segment_bounds(sequence_start, segment_index, total_duration):
    """Return the requested 3/5/10 boundary for a segment before keyframe effects."""
    if segment_index <= 0:
        rel_start = 0.0
        rel_end = min(3.0, max(0.0, total_duration - sequence_start))
    elif segment_index == 1:
        rel_start = 3.0
        rel_end = min(8.0, max(0.0, total_duration - sequence_start))
    else:
        rel_start = 8.0 + (segment_index - 2) * 10.0
        rel_end = min(rel_start + 10.0, max(0.0, total_duration - sequence_start))
    return sequence_start + rel_start, sequence_start + rel_end


def _start_sequence(s, start):
    start = max(0.0, min(float(start), max(0.0, s.duration - 0.02)))
    with s.lock:
        if s.full_done:
            return False
    _stop_sequence(s, "new playback path")

    with s.sequence_lock:
        generation = s.sequence_generation
        seqdir = s.cache_dir / f"sequence_{generation:04d}_{start:.3f}"
        seqdir.mkdir(parents=True, exist_ok=True)
        base = s.next_chunk_index
        s.sequence_dir = seqdir
        s.sequence_start = start
        s.sequence_index_base = base

    relative_duration = max(0.0, s.duration - start)
    cuts = _build_segment_times(relative_duration)
    cut_arg = ",".join(f"{x:.3f}" for x in cuts)

    if s.progressive_fmt:
        inputs = [s.progressive_fmt]
    else:
        inputs = [s.video_fmt, s.audio_fmt]

    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    for fmt in inputs:
        cmd += ["-ss", f"{start:.3f}"]
        cmd += ffmpeg_input_args(fmt)
        cmd += ["-i", stream_url(fmt)]

    cmd += ["-map", "0:v:0"]
    if len(inputs) == 1:
        cmd += ["-map", "0:a:0?"]
    else:
        cmd += ["-map", "1:a:0"]

    # V13 FIX: arbitrary stream-copy cuts were the root cause of the skipped
    # portions at chunk boundaries. H.264 can only be cleanly split on keyframes,
    # so the old -c:v copy + segment_times combination could produce a requested
    # 10s chunk containing only a fraction of the requested media.
    #
    # V13 uses ONE continuous encoder for the whole playback sequence. We force
    # keyframes at every requested boundary and then let the segment muxer write
    # separate MP4 files. Audio is encoded continuously by the SAME FFmpeg
    # process, so AAC is never restarted at every chunk (avoids v9's audio cuts).
    #
    # This makes the requested timeline real rather than merely metadata: each
    # segment actually contains the frames for its 3/5/10 second interval.
    encode_v = available_h264_encoder()
    if not encode_v:
        log("ERROR", f"{s.video_id}: FFmpeg has neither h264_amf nor libx264; cannot build exact playback chunks")
        return False
    encode_a = "aac"
    force_keys = cut_arg
    cmd += ["-c:v", encode_v]
    if encode_v == "h264_amf":
        # Fast AMD hardware encode for temporary playback chunks.
        cmd += ["-quality", "speed", "-rc", "cqp", "-qp_i", "23", "-qp_p", "25"]
    else:
        cmd += ["-preset", "ultrafast", "-crf", "23"]
    cmd += [
        "-pix_fmt", "yuv420p",
        "-force_key_frames", force_keys if force_keys else "0",
        "-c:a", encode_a,
        "-b:a", "160k",
        "-ar", "48000",
        "-ac", "2",
        "-avoid_negative_ts", "make_zero",
        "-f", "segment",
        "-segment_format", "mp4",
        "-segment_format_options", "movflags=+frag_keyframe+empty_moov+default_base_moof",
        "-reset_timestamps", "1",
        "-segment_start_number", "0",
    ]
    if cut_arg:
        cmd += ["-segment_times", cut_arg]
    else:
        cmd += ["-segment_time", "10"]
    cmd += [str(seqdir / "seg_%06d.mp4")]

    log("CHUNK", f"{s.video_id}: starting continuous sequence at {start:.3f}s; cuts=3/5/10s; segments={len(cuts)+1}")
    log("FFMPEG", f"{s.video_id}: sequence output={seqdir} relative_cuts={cut_arg or 'none'}")
    log("FFMPEG", f"{s.video_id}: V13 playback encoder={encode_v} + continuous AAC 160k; forced_keyframes={cut_arg or 'none'}")
    log("FFMPEG", f"{s.video_id}: video_format={s.video_fmt.get('format_id') if s.video_fmt else s.progressive_fmt.get('format_id') if s.progressive_fmt else None} audio_format={s.audio_fmt.get('format_id') if s.audio_fmt else 'embedded'}")
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.PIPE)
    except Exception as e:
        log("ERROR", f"{s.video_id}: failed to start FFmpeg sequence: {e}")
        return False
    with s.sequence_lock:
        s.sequence_process = p
    return True


def _sequence_process_alive(s):
    with s.sequence_lock:
        p = s.sequence_process
    return bool(p and p.poll() is None)


def _finalize_sequence_process(s):
    with s.sequence_lock:
        p = s.sequence_process
    if not p or p.poll() is None:
        return
    try:
        err = p.stderr.read() if p.stderr else b""
    except Exception:
        err = b""
    code = p.returncode
    with s.sequence_lock:
        if s.sequence_process is p:
            s.sequence_process = None
    _register_sequence_segments(s, stop_reason=None)
    if code != 0 and not s.chunk_stop.is_set() and not s.full_done:
        text = (err or b"").decode("utf-8", "replace")[-3000:]
        log("ERROR", f"{s.video_id}: continuous FFmpeg sequence exited {code}: {text}")


def find_chunk_covering(s, target, include_incomplete=True):
    with s.chunk_lock:
        candidates = []
        for c in s.chunks.values():
            if not include_incomplete and c.status != "complete":
                continue
            if c.start - 0.01 <= target < c.end - 0.01:
                candidates.append(c)
        if not candidates:
            return None
        candidates.sort(key=lambda x: (x.start, x.index), reverse=True)
        return candidates[0]


def delete_chunks(s):
    _stop_sequence(s, "full download finished")
    with s.chunk_lock:
        s.chunks.clear()
    # Remove every seek-path directory only after every FFmpeg sequence has
    # been stopped. This also cleans up old paths from previous far seeks.
    for seqdir in s.cache_dir.glob("sequence_*"):
        if seqdir.is_dir():
            shutil.rmtree(seqdir, ignore_errors=True)
    log("STATUS", f"{s.video_id}: full download finished; temporary playback chunks deleted")


def chunk_worker(s):
    """Maintain one continuous FFmpeg segment sequence per playback path."""
    last_diag = 0.0
    while not s.chunk_stop.is_set():
        with s.lock:
            if s.full_done:
                break
        _register_sequence_segments(s)
        _finalize_sequence_process(s)

        target = s.requested_target
        covered = find_chunk_covering(s, target, include_incomplete=True)
        now = time.time()
        if now - last_diag >= 1.0:
            with s.chunk_lock:
                summary = ", ".join(f"{c.index}:{c.start:.2f}-{c.end:.2f}:{c.status}" for c in sorted(s.chunks.values(), key=lambda x:x.index))
            with s.sequence_lock:
                seq_alive = bool(s.sequence_process and s.sequence_process.poll() is None)
                seqdir_now = str(s.sequence_dir) if s.sequence_dir else "none"
            log("DEBUG", f"{s.video_id}: target={target:.3f}s covered={covered.index if covered else None} current={s.current_playback_chunk} sequence_alive={seq_alive} seqdir={seqdir_now} chunks=[{summary}]")
            last_diag = now

        if covered is not None:
            with s.chunk_lock:
                s.current_playback_chunk = covered.index
            # If the target is covered by a segment already emitted, keep the
            # same sequence running so the forward buffer continues growing.
            if _sequence_process_alive(s):
                time.sleep(0.15)
                continue

        if not _sequence_process_alive(s):
            # If target is in an existing complete segment, there is no need to
            # create a duplicate sequence. Otherwise start a new one at target.
            if covered is None:
                _start_sequence(s, target)
            else:
                # Existing sequence ended; continue from its end if possible.
                with s.chunk_lock:
                    later = sorted(
                        [c for c in s.chunks.values() if c.status == "complete" and c.start >= covered.end - 0.01],
                        key=lambda c: c.start,
                    )
                if later:
                    pass
                else:
                    _start_sequence(s, covered.end)
        time.sleep(0.12)


def request_seek(s, target):
    target = max(0.0, min(float(target), max(0.0, s.duration - 0.02)))
    with s.lock:
        if s.full_done:
            s.requested_target = target
            return target, "full"

    existing = find_chunk_covering(s, target, include_incomplete=True)
    with s.chunk_lock:
        active_sequence = _sequence_process_alive(s)
        if existing:
            s.requested_target = target
            s.current_playback_chunk = existing.index
            s.chunk_wake.set()
            return target, existing.status

        # Far seek: stop the current continuous FFmpeg process. Its already
        # completed segment files remain untouched. The new sequence starts at
        # the requested target with its own continuous encoder timeline.
        if active_sequence:
            _stop_sequence(s, "far seek")
        s.requested_target = target
        s.current_playback_chunk = None
        s.chunk_wake.set()
        return target, "new"

def full_download_worker(s, info):
    outtmpl = str(s.cache_dir / "full.%(ext)s")
    try:
        if s.progressive_fmt:
            fmt_expr = str(s.progressive_fmt.get("format_id"))
        elif s.video_fmt and s.audio_fmt:
            fmt_expr = f"{s.video_fmt.get('format_id')}+{s.audio_fmt.get('format_id')}"
        else:
            fmt_expr = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"

        def hook(d):
            st = d.get("status")
            if st == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate")
                got = d.get("downloaded_bytes") or 0
                s.record_full(got, total, d.get("total_bytes") is not None)
            elif st == "finished":
                got = d.get("downloaded_bytes") or 0
                total = d.get("total_bytes") or got
                s.record_full(got, total, d.get("total_bytes") is not None)

        opts = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "outtmpl": outtmpl,
            "format": fmt_expr,
            "merge_output_format": "mp4",
            "progress_hooks": [hook],
            "retries": 5,
            "fragment_retries": 5,
            "cookiefile": YTDLP_COOKIE_FILE,
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([s.original_url])

        candidates = sorted(s.cache_dir.glob("full.*"), key=lambda p: p.stat().st_size, reverse=True)
        candidates = [p for p in candidates if p.suffix.lower() not in (".part", ".ytdl")]
        if not candidates:
            raise RuntimeError("yt-dlp finished but full output was not found")
        final = candidates[0]
        with s.lock:
            s.full_file = final
            # Stop chunk production before publishing full_done. This closes the
            # race where the chunk worker could wake up between cleanup steps
            # and create a fresh 3/5/10 sequence after the chunks were deleted.
            s.chunk_stop.set()
            s.full_done = True
            s.full_downloaded = final.stat().st_size
            s.full_total = final.stat().st_size
            s.full_exact = True
        # Once the unrestricted full file exists, the chunk pipeline is no longer
        # needed. Stop the worker BEFORE deleting its files so it cannot recreate
        # chunks after cleanup (the v7 race that produced a new sequence after
        # "temporary playback chunks deleted").
        s.chunk_stop.set()
        s.chunk_wake.set()
        with s.chunk_lock:
            active = s.active_chunk
        if active:
            kill_chunk_process(active, pause=False)
        log("STATUS", f"{s.video_id}: FULL download complete ({fmt_size(final.stat().st_size)})")
        delete_chunks(s)
    except Exception as e:
        with s.lock:
            s.error_message = f"Full download failed: {e}"
        log("ERROR", f"{s.video_id}: full download failed: {e}")
        log("ERROR", traceback.format_exc())


def resolve_and_start(url):
    log("INFO", f"Resolving metadata for submitted URL ({redact_url(url)})")
    info = extract_info(url)
    vid = sanitize_video_id(info.get("id"))
    title = info.get("title") or "Untitled"
    duration = float(info.get("duration") or 0)
    with SESSIONS_LOCK:
        old = SESSIONS.get(vid)
        if old and old.status not in ("error",):
            return old
        s = DownloadSession(vid, title, duration)
        s.original_url = url
        SESSIONS[vid] = s

    progressive, video, audio = choose_streams(info)
    s.progressive_fmt = progressive
    s.video_fmt = video
    s.audio_fmt = audio

    if progressive:
        s.mode = "progressive + chunked"
        s.mime = codec_string(progressive, progressive)
        # A progressive source already has both tracks. The stream copy chunk
        # muxer can use it directly.
        log("INFO", f"{vid}: progressive source {progressive.get('format_id')} selected")
    elif video and audio and FFMPEG_AVAILABLE:
        s.mode = "adaptive + chunked"
        s.mime = codec_string(video, audio)
        alang = audio.get("language") or "unknown"
        anote = audio.get("format_note") or ""
        log("INFO", f"{vid}: adaptive sources video={video.get('format_id')} {video.get('height')}p "
                    f"audio={audio.get('format_id')} lang={alang} note={anote!r} abr={audio.get('abr')}")
    else:
        s.status = "error"
        s.error_message = "Could not find an MP4 source suitable for local chunking, and FFmpeg is unavailable."
        return s

    s.full_thread = threading.Thread(target=full_download_worker, args=(s, info), daemon=True)
    s.full_thread.start()
    s.chunk_worker = threading.Thread(target=chunk_worker, args=(s,), daemon=True)
    s.chunk_worker.start()
    s.status = "downloading"
    s.chunk_wake.set()
    return s


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "LocalYouTubePlayer/12"

    def log_message(self, fmt, *args):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, msg, code=400):
        self._json({"error": msg}, code)

    def _body(self):
        n = int(self.headers.get("Content-Length", "0"))
        if n > MAX_REQUEST_BODY:
            raise ValueError("request body too large")
        return json.loads(self.rfile.read(n).decode() or "{}")

    def do_GET(self):
        p = urllib.parse.urlsplit(self.path)
        try:
            if p.path == "/":
                return self._index()
            if p.path.startswith("/api/status/"):
                return self._status(p.path.split("/")[-1])
            if p.path.startswith("/api/chunks/"):
                return self._chunks(p.path.split("/")[-1])
            if p.path.startswith("/api/full/"):
                return self._full(p.path.split("/")[-1])
            if p.path.startswith("/media/full/"):
                return self._serve_named_file(p.path[len("/media/full/"):])
            if p.path.startswith("/media/chunk/"):
                return self._serve_chunk(p.path[len("/media/chunk/"):])
            self._error("not found", 404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            log("ERROR", f"GET {self.path}: {e}")
            try:
                self._error("internal server error", 500)
            except Exception:
                pass

    def do_POST(self):
        p = urllib.parse.urlsplit(self.path).path
        try:
            if p == "/api/load":
                return self._load()
            if p == "/api/seek":
                return self._seek()
            self._error("not found", 404)
        except ValueError as e:
            self._error(str(e), 400)
        except Exception as e:
            log("ERROR", f"POST {p}: {e}")
            self._error("internal server error", 500)

    def _index(self):
        body = (Path(__file__).resolve().parent / "index.html").read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _get_session(self, vid):
        vid = sanitize_video_id(vid)
        with SESSIONS_LOCK:
            s = SESSIONS.get(vid)
        if not s:
            raise ValueError("unknown video id")
        return s

    def _load(self):
        if not YTDLP_AVAILABLE:
            return self._error("yt-dlp is not installed. Run: pip install -U yt-dlp", 500)
        if not FFMPEG_AVAILABLE:
            return self._error("FFmpeg is required for chunked playback. Install FFmpeg and put it on PATH.", 500)
        data = self._body()
        url = (data.get("url") or "").strip()
        if not url or not is_youtube_url(url):
            return self._error("enter a valid YouTube URL", 400)
        log("INFO", f"URL submitted: {redact_url(url)}")
        s = resolve_and_start(url)
        self._json({"video_id": s.video_id, "title": s.title, "duration": s.duration, "mode": s.mode,
                    "status": s.status, "error": s.error_message, "mime": s.mime})

    def _status(self, vid):
        s = self._get_session(vid)
        with s.lock:
            pct = s.progress()
            full_done = s.full_done
            data = {
                "video_id": s.video_id, "title": s.title, "duration": s.duration,
                "status": s.status, "mode": s.mode, "error": s.error_message,
                "full_downloaded": s.full_downloaded, "full_total": s.full_total,
                "full_percent": pct, "full_rate_bps": s.full_rate, "full_done": full_done,
                "mime": s.mime, "requested_target": s.requested_target,
                "current_playback_chunk": s.current_playback_chunk,
            }
        with s.chunk_lock:
            chunks = [c.to_dict() for c in sorted(s.chunks.values(), key=lambda c: (c.start, c.index))]
            active = s.active_chunk.to_dict() if s.active_chunk else None
        data["chunks"] = chunks
        data["active_chunk"] = active
        data["coverage_end"] = max([c["end"] for c in chunks if c["status"] == "complete"] + [0.0])
        self._json(data)

    def _chunks(self, vid):
        s = self._get_session(vid)
        with s.chunk_lock:
            self._json({"mime": s.mime, "chunks": [c.to_dict() for c in sorted(s.chunks.values(), key=lambda c: (c.start, c.index))]})

    def _full(self, vid):
        s = self._get_session(vid)
        self._json({"ready": bool(s.final_file and s.full_done), "file": s.final_file.name if s.final_file else None})

    def _seek(self):
        d = self._body()
        s = self._get_session(d.get("video_id"))
        t = float(d.get("time", 0))
        target, action = request_seek(s, t)
        log("SEEK", f"{s.video_id}: seek -> {target:.2f}s action={action} (full download continues)")
        self._json({"ok": True, "target": target, "action": action})

    def _serve_named_file(self, suffix):
        parts = suffix.strip("/").split("/")
        vid = sanitize_video_id(parts[0])
        s = self._get_session(vid)
        path = s.final_file
        if not path or not path.exists():
            return self._error("full video is not ready", 404)
        self._range_file(path)

    def _serve_chunk(self, suffix):
        parts = suffix.strip("/").split("/")
        if len(parts) != 2:
            return self._error("invalid chunk path", 400)
        vid = sanitize_video_id(parts[0])
        try:
            idx = int(parts[1])
        except ValueError:
            return self._error("invalid chunk index", 400)
        s = self._get_session(vid)
        with s.chunk_lock:
            c = s.chunks.get(idx)
            path = c.path if c else None
            ready = bool(c and c.status == "complete" and path.exists())
        if not ready:
            return self._error("chunk is not ready", 404)
        self._range_file(path)

    def _range_file(self, path):
        total = path.stat().st_size
        rh = self.headers.get("Range")
        start, end, code = 0, total - 1, 200
        if rh:
            m = re.match(r"bytes=(\d*)-(\d*)", rh)
            if not m:
                return self._error("malformed Range", 416)
            a, b = m.groups()
            start = int(a) if a else max(0, total - int(b))
            end = int(b) if b else total - 1
            end = min(end, total - 1)
            if start < 0 or start >= total or start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{total}")
                self.end_headers()
                return
            code = 206
        length = end - start + 1
        ctype = "video/mp4"
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        if code == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
        self.end_headers()
        with open(path, "rb") as f:
            f.seek(start)
            left = length
            while left:
                b = f.read(min(256 * 1024, left))
                if not b:
                    break
                self.wfile.write(b)
                left -= len(b)


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def find_port():
    return PORT

def main():
    if not (Path(__file__).resolve().parent / "index.html").exists():
        print("index.html is missing")
        sys.exit(1)
    if not YTDLP_AVAILABLE:
        print("WARNING: install yt-dlp with: pip install -U yt-dlp")
    if not FFMPEG_AVAILABLE:
        print("WARNING: FFmpeg is required for chunked playback")
    port = find_port()
    httpd = Server((HOST, port), Handler)
    url = f"http://127.0.0.1:{port}/"
    print("=" * 70)
    print(" Local YouTube-style Video Player v10")
    print("=" * 70)
    print(f" Python version     : {sys.version.split()[0]}")
    print(f" yt-dlp available   : {'yes' if YTDLP_AVAILABLE else 'NO'}")
    print(f" ffmpeg available   : {'yes' if FFMPEG_AVAILABLE else 'NO'}")
    print(f" Host / Port        : {HOST}:{port}")
    print(f" Local URL          : {url}")
    print(f" Videos cache dir   : {VIDEOS_DIR}")
    print(" Architecture       : full download + continuous FFmpeg 3/5/10-second segment sequences (source-timeline locked)")
    print("=" * 70)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    log("STATUS", f"Server started on {HOST}:{port}")   
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log("STATUS", "Stopping server")
        httpd.shutdown()


if __name__ == "__main__":
    main()
