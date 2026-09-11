#!/usr/bin/env python3
"""
Local YouTube-style player v13.

Playback architecture:
  * The complete video downloads continuously with yt-dlp in the background.
  * Playback is built from independent fragmented-MP4 chunk files.
  * Chunk sizes are 3s, then 5s, then 10s forever.
  * The browser uses Media Source Extensions to append chunks into one timeline.
  * Seeking inside an existing chunk does not start a new download.
  * Seeking outside existing coverage starts a new playback sequence.
  * When the full download completes, temporary chunks are deleted and the
    browser can switch to the complete MP4.

Render deployment:
  * HOST = 0.0.0.0
  * PORT = Render's supplied PORT environment variable
  * YouTube cookies are loaded from:
        /etc/secrets/cookies.txt
"""

import contextlib
import http.server
import json
import os
import re
import shutil
import socketserver
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
from pathlib import Path


# ============================================================================
# CONFIGURATION
# ============================================================================

HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", 5000))

BASE_DIR = Path(__file__).resolve().parent
VIDEOS_DIR = BASE_DIR / "videos"
VIDEOS_DIR.mkdir(exist_ok=True)

# Render Secret File:
#   Filename: cookies.txt
#   Runtime path: /etc/secrets/cookies.txt
YTDLP_COOKIE_FILE = os.environ.get(
    "YTDLP_COOKIE_FILE",
    str(BASE_DIR / "cookies.txt")
)

# Optional. If set in Render environment variables, this can help yt-dlp
# maintain the same browser-like user agent as the cookies.
YTDLP_USER_AGENT = os.environ.get("YTDLP_USER_AGENT", "").strip()

CHUNK_SIZES = (3.0, 5.0, 10.0)
FFMPEG_TIMEOUT = 180
FFPROBE_TIMEOUT = 30
MAX_REQUEST_BODY = 8192
POLL_INTERVAL = 0.15

_ENCODER_CACHE = None

_LOG_LOCK = threading.Lock()

SESSIONS = {}
SESSIONS_LOCK = threading.Lock()

_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{4,32}$")

_YOUTUBE_HOST_RE = re.compile(
    r"^(www\.|m\.|music\.)?(youtube\.com|youtu\.be)$",
    re.I
)


# ============================================================================
# LOGGING
# ============================================================================

def log(level, msg):
    with _LOG_LOCK:
        print(
            f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] "
            f"{level:<8} {msg}",
            flush=True
        )


def redact_url(url):
    try:
        p = urllib.parse.urlsplit(url)

        pairs = urllib.parse.parse_qsl(
            p.query,
            keep_blank_values=True
        )

        secret = {
            "signature",
            "sig",
            "token",
            "auth",
            "key",
            "authorization",
            "sp",
        }

        pairs = [
            (
                k,
                "REDACTED" if k.lower() in secret else v
            )
            for k, v in pairs
        ]

        return urllib.parse.urlunsplit(
            (
                p.scheme,
                p.netloc,
                p.path,
                urllib.parse.urlencode(pairs),
                ""
            )
        )

    except Exception:
        return "<url>"


# ============================================================================
# DEPENDENCY CHECKS
# ============================================================================

def check_yt_dlp():
    try:
        import yt_dlp
        return True, yt_dlp
    except ImportError:
        return False, None


def check_ffmpeg():
    return shutil.which("ffmpeg") is not None


YTDLP_AVAILABLE, yt_dlp = check_yt_dlp()
FFMPEG_AVAILABLE = check_ffmpeg()


def available_h264_encoder():
    """
    Prefer AMD AMF if available.

    Render normally will not have an AMD GPU, so Render will generally use
    libx264 instead.
    """

    global _ENCODER_CACHE

    if _ENCODER_CACHE is not None:
        return _ENCODER_CACHE

    try:
        p = subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-encoders"
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=10
        )

        text = p.stdout or ""

        if (
            " h264_amf " in text
            or " h264_amf\n" in text
        ):
            _ENCODER_CACHE = "h264_amf"

        elif (
            " libx264 " in text
            or " libx264\n" in text
        ):
            _ENCODER_CACHE = "libx264"

        else:
            _ENCODER_CACHE = None

    except Exception:
        _ENCODER_CACHE = "libx264"

    return _ENCODER_CACHE


# ============================================================================
# GENERAL HELPERS
# ============================================================================

def sanitize_video_id(v):
    if not v or not _VIDEO_ID_RE.match(v):
        raise ValueError("invalid video id")

    return v


def is_youtube_url(url):
    try:
        p = urllib.parse.urlsplit(url)

        return (
            p.scheme in ("http", "https")
            and bool(_YOUTUBE_HOST_RE.match(p.netloc))
        )

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

    return f"{n:.1f} GiB"


def atomic_json(path, data):
    tmp = path.with_suffix(path.suffix + ".tmp")

    tmp.write_text(
        json.dumps(data, indent=2),
        encoding="utf-8"
    )

    os.replace(tmp, path)


# ============================================================================
# YT-DLP
# ============================================================================

def yt_dlp_options(base=None):
    """
    Build common yt-dlp options.

    The cookie file is only added if it actually exists. This means the same
    server.py can still run locally without a cookies.txt file.
    """

    opts = dict(base or {})

    cookie_path = Path(YTDLP_COOKIE_FILE)

    if cookie_path.is_file():
        opts["cookiefile"] = str(cookie_path)

        log(
            "YTDLP",
            f"Using YouTube cookies from {cookie_path}"
        )

    else:
        log(
            "YTDLP",
            f"No cookie file found at {cookie_path}"
        )

    if YTDLP_USER_AGENT:
        opts["http_headers"] = {
            "User-Agent": YTDLP_USER_AGENT
        }

    return opts


def extract_info(url):
    opts = yt_dlp_options(
        {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "skip_download": True,
        }
    )

    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(
            url,
            download=False
        )


# ============================================================================
# FORMAT SELECTION
# ============================================================================

def is_h264(f):
    return (
        (f.get("vcodec") or "none")
        .lower()
        .startswith("avc1")
    )


def is_aac(f):
    return (
        (f.get("acodec") or "none")
        .lower()
        .startswith("mp4a")
    )


def choose_streams(info):
    """
    Pick a browser-friendly H.264/AAC source pair.

    Audio selection prefers English and original/default tracks.
    """

    formats = info.get("formats", [])

    progressive = []
    videos = []
    audios = []

    def lang_score(f):
        lang = str(
            f.get("language") or ""
        ).lower()

        note = str(
            f.get("format_note") or ""
        ).lower()

        name = str(
            f.get("language_preference") or ""
        ).lower()

        if lang in (
            "en",
            "en-us",
            "en-gb",
            "en-ca",
            "en-au",
        ):
            score = 1000

        elif lang.startswith("en-"):
            score = 950

        elif lang:
            score = 100

        else:
            score = 300

        if "original" in note:
            score += 350

        if "default" in note:
            score += 250

        if "original" in name:
            score += 150

        if "dub" in note:
            score -= 400

        if "dubbed" in note:
            score -= 400

        if "auto-dub" in note:
            score -= 500

        return score

    for f in formats:

        protocol = (
            f.get("protocol") or ""
        ).lower()

        if protocol not in (
            "http",
            "https",
            "http_dash_segments",
        ):
            continue

        ext = (
            f.get("ext") or ""
        ).lower()

        if ext not in ("mp4", "m4a"):
            continue

        if not f.get("url"):
            continue

        vc = (
            f.get("vcodec") or "none"
        ).lower()

        ac = (
            f.get("acodec") or "none"
        ).lower()

        if (
            vc != "none"
            and ac != "none"
            and ext == "mp4"
        ):
            progressive.append(f)

        elif (
            vc != "none"
            and ac == "none"
            and is_h264(f)
            and ext == "mp4"
        ):
            videos.append(f)

        elif (
            vc == "none"
            and ac != "none"
            and is_aac(f)
            and ext in ("m4a", "mp4")
        ):
            audios.append(f)

    progressive.sort(
        key=lambda f: (
            f.get("height") or 0,
            f.get("tbr") or 0,
        ),
        reverse=True
    )

    videos.sort(
        key=lambda f: (
            f.get("height") or 0,
            f.get("tbr") or 0,
        ),
        reverse=True
    )

    audios.sort(
        key=lambda f: (
            lang_score(f),
            f.get("abr") or 0,
            f.get("tbr") or 0,
            f.get("filesize") or 0,
        ),
        reverse=True
    )

    if videos and audios:
        return (
            None,
            videos[0],
            audios[0]
        )

    return (
        progressive[0] if progressive else None,
        None,
        None
    )


# ============================================================================
# MEDIA / FFMPEG HELPERS
# ============================================================================

def header_blob(fmt):
    headers = (
        fmt or {}
    ).get("http_headers") or {}

    lines = []

    for k, v in headers.items():

        if k.lower() in (
            "range",
            "content-length",
            "host",
        ):
            continue

        lines.append(
            f"{k}: {v}"
        )

    return (
        "\r\n".join(lines)
        + ("\r\n" if lines else "")
    )


def ffmpeg_input_args(fmt):
    blob = header_blob(fmt)

    if blob:
        return [
            "-headers",
            blob
        ]

    return []


def stream_url(fmt):
    return (
        fmt or {}
    ).get("url")


def codec_string(video_fmt, audio_fmt):
    v = (
        (video_fmt or {}).get("vcodec")
        or "avc1.4d401f"
    )

    a = (
        (audio_fmt or {}).get("acodec")
        or "mp4a.40.2"
    )

    v = v.split(" ", 1)[0]
    a = a.split(" ", 1)[0]

    return (
        f'video/mp4; codecs="{v},{a}"'
    )


def probe_media_details(path):
    try:
        p = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                (
                    "format=duration,start_time:"
                    "stream=index,codec_type,start_time,duration"
                ),
                "-of",
                "json",
                str(path),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=FFPROBE_TIMEOUT,
        )

        if p.returncode != 0:
            return None

        data = json.loads(
            p.stdout or "{}"
        )

        streams = data.get("streams") or []

        video = next(
            (
                x for x in streams
                if x.get("codec_type") == "video"
            ),
            {}
        )

        audio = next(
            (
                x for x in streams
                if x.get("codec_type") == "audio"
            ),
            {}
        )

        return {
            "streams": len(streams),
            "format_duration": (
                data.get("format", {})
                .get("duration")
            ),
            "format_start": (
                data.get("format", {})
                .get("start_time")
            ),
            "video_start": video.get("start_time"),
            "audio_start": audio.get("start_time"),
            "video_duration": video.get("duration"),
            "audio_duration": audio.get("duration"),
        }

    except Exception as e:
        log(
            "DEBUG",
            f"ffprobe details failed for "
            f"{path.name}: {e}"
        )

        return None


def probe_media_duration(path):
    try:
        p = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=FFPROBE_TIMEOUT,
        )

        if p.returncode == 0:

            value = float(
                (p.stdout or "").strip()
            )

            if value > 0:
                return value

    except Exception:
        pass

    return None


# ============================================================================
# CHUNK MODEL
# ============================================================================

class Chunk:

    def __init__(
        self,
        index,
        start,
        end,
        path
    ):
        self.index = int(index)
        self.start = float(start)
        self.end = float(end)
        self.path = Path(path)

        self.status = (
            "complete"
            if self.path.exists()
            else "pending"
        )

        self.error = None
        self.process = None
        self.lock = threading.RLock()
        self.generation = 0
        self.actual_duration = None

    @property
    def duration(self):
        return max(
            0.0,
            self.end - self.start
        )

    def to_dict(self):

        with self.lock:

            size = (
                self.path.stat().st_size
                if self.path.exists()
                else 0
            )

            return {
                "index": self.index,
                "start": self.start,
                "end": self.end,
                "duration": self.duration,
                "actual_duration": self.actual_duration,
                "status": self.status,
                "file": (
                    self.path.name
                    if self.path.exists()
                    else None
                ),
                "size": size,
                "error": self.error,
            }


# ============================================================================
# DOWNLOAD SESSION
# ============================================================================

class DownloadSession:

    def __init__(
        self,
        video_id,
        title,
        duration
    ):
        self.video_id = video_id
        self.title = title
        self.duration = float(
            duration or 0
        )

        self.cache_dir = (
            VIDEOS_DIR / video_id
        )

        self.cache_dir.mkdir(
            parents=True,
            exist_ok=True
        )

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

        self.requested_target = 0.0
        self.current_playback_chunk = None

        self.sequence_generation = 0
        self.sequence_process = None
        self.sequence_dir = None
        self.sequence_start = 0.0
        self.sequence_index_base = 0
        self.next_chunk_index = 0

        self.sequence_lock = threading.RLock()

    @property
    def final_file(self):

        if (
            self.full_file
            and self.full_file.exists()
        ):
            return self.full_file

        p = self.cache_dir / "full.mp4"

        if (
            p.exists()
            and self.full_done
        ):
            return p

        return None

    def record_full(
        self,
        n,
        total=None,
        exact=False
    ):
        now = time.time()

        with self.lock:

            self.full_downloaded = int(
                n or 0
            )

            if total:
                self.full_total = int(total)

            self.full_exact = bool(exact)

            self._rate.append(
                (
                    now,
                    self.full_downloaded
                )
            )

            cutoff = now - 5

            self._rate = [
                x for x in self._rate
                if x[0] >= cutoff
            ]

            if len(self._rate) >= 2:

                a = self._rate[0]
                b = self._rate[-1]

                self.full_rate = max(
                    0,
                    (
                        b[1] - a[1]
                    ) / max(
                        0.5,
                        b[0] - a[0]
                    )
                )

    def progress(self):

        with self.lock:

            if self.full_total:

                return min(
                    100.0,
                    (
                        self.full_downloaded
                        / self.full_total
                        * 100
                    )
                )

            return None


# ============================================================================
# CHUNK PATH / TIMELINE
# ============================================================================

def chunk_path(
    s,
    index,
    start,
    end
):
    return (
        s.cache_dir
        / (
            f"chunk_{index:06d}_"
            f"{start:.3f}_"
            f"{end:.3f}.mp4"
        )
    )


def _build_segment_times(relative_duration):
    """
    3 seconds, then 5 seconds, then 10 seconds forever.
    """

    cuts = []

    t = 3.0

    if relative_duration <= 3.0:
        return cuts

    cuts.append(t)

    t = 8.0

    if relative_duration <= 8.0:
        return cuts

    cuts.append(t)

    while (
        t + 10.0
        < relative_duration - 0.02
    ):
        t += 10.0
        cuts.append(t)

    return cuts


def _expected_segment_bounds(
    sequence_start,
    segment_index,
    total_duration
):
    """
    Return requested source timeline boundaries.
    """

    if segment_index <= 0:

        rel_start = 0.0

        rel_end = min(
            3.0,
            max(
                0.0,
                total_duration - sequence_start
            )
        )

    elif segment_index == 1:

        rel_start = 3.0

        rel_end = min(
            8.0,
            max(
                0.0,
                total_duration - sequence_start
            )
        )

    else:

        rel_start = (
            8.0
            + (
                segment_index - 2
            ) * 10.0
        )

        rel_end = min(
            rel_start + 10.0,
            max(
                0.0,
                total_duration - sequence_start
            )
        )

    return (
        sequence_start + rel_start,
        sequence_start + rel_end
    )


# ============================================================================
# CHUNK PROCESS MANAGEMENT
# ============================================================================

def kill_chunk_process(
    chunk,
    pause=False
):
    """
    Compatibility helper for older chunk-worker logic.

    V13 primarily uses continuous FFmpeg sequences, but this safely handles
    any legacy Chunk process if one exists.
    """

    if chunk is None:
        return

    try:

        p = chunk.process

        if not p:
            return

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
                    p.wait(timeout=2)
                except Exception:
                    pass

        chunk.process = None

    except Exception as e:

        log(
            "DEBUG",
            f"chunk process cleanup failed: {e}"
        )


def _register_sequence_segments(
    s,
    stop_reason=None
):
    """
    Register FFmpeg-created segment files on the SOURCE timeline.

    Physical encoded duration is stored separately for diagnostics.
    Requested source timeline remains authoritative.
    """

    with s.sequence_lock:

        seqdir = s.sequence_dir
        base = s.sequence_index_base
        seq_start = s.sequence_start

    if (
        not seqdir
        or not seqdir.exists()
    ):
        return

    files = sorted(
        seqdir.glob("seg_*.mp4")
    )

    for path in files:

        try:
            n = int(
                path.stem.split("_")[-1]
            )

        except Exception:
            continue

        idx = base + n

        with s.chunk_lock:

            if (
                idx in s.chunks
                and s.chunks[idx].status
                == "complete"
            ):
                continue

        actual = probe_media_duration(
            path
        )

        if actual is None:
            continue

        (
            expected_start,
            expected_end
        ) = _expected_segment_bounds(
            seq_start,
            n,
            s.duration
        )

        actual_probe = probe_media_details(
            path
        )

        with s.chunk_lock:

            if (
                idx in s.chunks
                and s.chunks[idx].status
                == "complete"
            ):
                continue

            start = expected_start
            end = expected_end

            if end <= start + 0.001:
                continue

            c = Chunk(
                idx,
                start,
                end,
                path
            )

            c.actual_duration = actual
            c.status = "complete"

            s.chunks[idx] = c

            s.next_chunk_index = max(
                s.next_chunk_index,
                idx + 1
            )

            if (
                s.current_playback_chunk
                is None
            ):
                s.current_playback_chunk = idx

        drift_start = (
            start - expected_start
        )

        drift_end = (
            end - expected_end
        )

        log(
            "CHUNK",
            (
                f"{s.video_id}: "
                f"segment {idx} READY "
                f"(SOURCE TIMELINE AUTHORITY)"
            )
        )

        log(
            "TIMELINE",
            (
                f"{s.video_id}: "
                f"seg={idx} "
                f"REQUESTED "
                f"[{expected_start:.3f},"
                f"{expected_end:.3f}] "
                f"ACTUAL "
                f"[{start:.3f},"
                f"{end:.3f}] "
                f"duration={actual:.3f}s "
                f"drift_start={drift_start:+.3f}s "
                f"drift_end={drift_end:+.3f}s"
            )
        )

        if actual_probe:

            log(
                "MEDIA",
                (
                    f"{s.video_id}: "
                    f"seg={idx} "
                    f"streams="
                    f"{actual_probe.get('streams')} "
                    f"format_duration="
                    f"{actual_probe.get('format_duration')} "
                    f"format_start="
                    f"{actual_probe.get('format_start')} "
                    f"video_start="
                    f"{actual_probe.get('video_start')} "
                    f"audio_start="
                    f"{actual_probe.get('audio_start')} "
                    f"video_dur="
                    f"{actual_probe.get('video_duration')} "
                    f"audio_dur="
                    f"{actual_probe.get('audio_duration')}"
                )
            )

        expected_duration = max(
            0.0,
            expected_end - expected_start
        )

        duration_error = (
            actual - expected_duration
        )

        if abs(duration_error) > 0.15:

            log(
                "WARNING",
                (
                    f"{s.video_id}: "
                    f"PHYSICAL DURATION MISMATCH "
                    f"seg={idx}: "
                    f"expected="
                    f"{expected_duration:.3f}s "
                    f"actual="
                    f"{actual:.3f}s "
                    f"error="
                    f"{duration_error:+.3f}s"
                )
            )

        if abs(duration_error) > 0.75:

            log(
                "ERROR",
                (
                    f"{s.video_id}: "
                    f"CHUNK MAY BE INVALID "
                    f"seg={idx}: "
                    f"expected "
                    f"{expected_duration:.3f}s "
                    f"but file contains "
                    f"{actual:.3f}s"
                )
            )

        if abs(drift_end) > 0.75:

            log(
                "WARNING",
                (
                    f"{s.video_id}: "
                    f"LARGE TIMELINE DRIFT "
                    f"on segment {idx}: "
                    f"{drift_end:+.3f}s"
                )
            )


def _stop_sequence(
    s,
    reason="stop",
    graceful=False
):
    with s.sequence_lock:

        p = s.sequence_process

        s.sequence_process = None

        s.sequence_generation += 1

    if (
        p
        and p.poll() is None
    ):

        log(
            "CHUNK",
            (
                f"{s.video_id}: "
                f"stopping FFmpeg sequence "
                f"({reason})"
            )
        )

        if graceful:

            try:

                if p.stdin:

                    p.stdin.write(
                        b"q\n"
                    )

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

    _register_sequence_segments(
        s,
        stop_reason=reason
    )


# ============================================================================
# START CONTINUOUS FFMPEG SEQUENCE
# ============================================================================

def _start_sequence(
    s,
    start
):
    start = max(
        0.0,
        min(
            float(start),
            max(
                0.0,
                s.duration - 0.02
            )
        )
    )

    with s.lock:

        if s.full_done:
            return False

    _stop_sequence(
        s,
        "new playback path"
    )

    with s.sequence_lock:

        generation = (
            s.sequence_generation
        )

        seqdir = (
            s.cache_dir
            / (
                f"sequence_"
                f"{generation:04d}_"
                f"{start:.3f}"
            )
        )

        seqdir.mkdir(
            parents=True,
            exist_ok=True
        )

        base = s.next_chunk_index

        s.sequence_dir = seqdir
        s.sequence_start = start
        s.sequence_index_base = base

    relative_duration = max(
        0.0,
        s.duration - start
    )

    cuts = _build_segment_times(
        relative_duration
    )

    cut_arg = ",".join(
        f"{x:.3f}"
        for x in cuts
    )

    if s.progressive_fmt:

        inputs = [
            s.progressive_fmt
        ]

    else:

        inputs = [
            s.video_fmt,
            s.audio_fmt
        ]

    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
    ]

    for fmt in inputs:

        cmd += [
            "-ss",
            f"{start:.3f}"
        ]

        cmd += ffmpeg_input_args(
            fmt
        )

        cmd += [
            "-i",
            stream_url(fmt)
        ]

    cmd += [
        "-map",
        "0:v:0"
    ]

    if len(inputs) == 1:

        cmd += [
            "-map",
            "0:a:0?"
        ]

    else:

        cmd += [
            "-map",
            "1:a:0"
        ]

    encode_v = available_h264_encoder()

    if not encode_v:

        log(
            "ERROR",
            (
                f"{s.video_id}: "
                f"FFmpeg has neither "
                f"h264_amf nor libx264"
            )
        )

        return False

    encode_a = "aac"

    cmd += [
        "-c:v",
        encode_v,
    ]

    if encode_v == "h264_amf":

        cmd += [
            "-quality",
            "speed",
            "-rc",
            "cqp",
            "-qp_i",
            "23",
            "-qp_p",
            "25",
        ]

    else:

        cmd += [
            "-preset",
            "ultrafast",
            "-crf",
            "23",
        ]

    cmd += [
        "-pix_fmt",
        "yuv420p",
        "-force_key_frames",
        cut_arg if cut_arg else "0",
        "-c:a",
        encode_a,
        "-b:a",
        "160k",
        "-ar",
        "48000",
        "-ac",
        "2",
        "-avoid_negative_ts",
        "make_zero",
        "-f",
        "segment",
        "-segment_format",
        "mp4",
        "-segment_format_options",
        (
            "movflags="
            "+frag_keyframe"
            "+empty_moov"
            "+default_base_moof"
        ),
        "-reset_timestamps",
        "1",
        "-segment_start_number",
        "0",
    ]

    if cut_arg:

        cmd += [
            "-segment_times",
            cut_arg
        ]

    else:

        cmd += [
            "-segment_time",
            "10"
        ]

    cmd += [
        str(
            seqdir
            / "seg_%06d.mp4"
        )
    ]

    log(
        "CHUNK",
        (
            f"{s.video_id}: "
            f"starting continuous sequence "
            f"at {start:.3f}s; "
            f"cuts=3/5/10s; "
            f"segments={len(cuts) + 1}"
        )
    )

    log(
        "FFMPEG",
        (
            f"{s.video_id}: "
            f"sequence output={seqdir} "
            f"relative_cuts="
            f"{cut_arg or 'none'}"
        )
    )

    log(
        "FFMPEG",
        (
            f"{s.video_id}: "
            f"playback encoder={encode_v} "
            f"+ continuous AAC 160k; "
            f"forced_keyframes="
            f"{cut_arg or 'none'}"
        )
    )

    video_id = (
        s.video_fmt.get("format_id")
        if s.video_fmt
        else (
            s.progressive_fmt.get("format_id")
            if s.progressive_fmt
            else None
        )
    )

    audio_id = (
        s.audio_fmt.get("format_id")
        if s.audio_fmt
        else "embedded"
    )

    log(
        "FFMPEG",
        (
            f"{s.video_id}: "
            f"video_format={video_id} "
            f"audio_format={audio_id}"
        )
    )

    try:

        p = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            stdin=subprocess.PIPE,
        )

    except Exception as e:

        log(
            "ERROR",
            (
                f"{s.video_id}: "
                f"failed to start FFmpeg "
                f"sequence: {e}"
            )
        )

        return False

    with s.sequence_lock:
        s.sequence_process = p

    return True


def _sequence_process_alive(s):

    with s.sequence_lock:

        p = s.sequence_process

    return bool(
        p
        and p.poll() is None
    )


def _finalize_sequence_process(s):

    with s.sequence_lock:
        p = s.sequence_process

    if not p:
        return

    if p.poll() is None:
        return

    try:

        if p.stderr:

            err = p.stderr.read()

        else:

            err = b""

    except Exception:

        err = b""

    code = p.returncode

    with s.sequence_lock:

        if s.sequence_process is p:
            s.sequence_process = None

    _register_sequence_segments(
        s,
        stop_reason=None
    )

    if (
        code != 0
        and not s.chunk_stop.is_set()
        and not s.full_done
    ):

        if isinstance(err, bytes):

            text = err.decode(
                "utf-8",
                "replace"
            )[-3000:]

        else:

            text = str(err)[-3000:]

        log(
            "ERROR",
            (
                f"{s.video_id}: "
                f"continuous FFmpeg sequence "
                f"exited {code}: {text}"
            )
        )


# ============================================================================
# CHUNK LOOKUP
# ============================================================================

def find_chunk_covering(
    s,
    target,
    include_incomplete=True
):
    with s.chunk_lock:

        candidates = []

        for c in s.chunks.values():

            if (
                not include_incomplete
                and c.status != "complete"
            ):
                continue

            if (
                c.start - 0.01
                <= target
                < c.end - 0.01
            ):
                candidates.append(c)

        if not candidates:
            return None

        candidates.sort(
            key=lambda x: (
                x.start,
                x.index
            ),
            reverse=True
        )

        return candidates[0]


# ============================================================================
# DELETE TEMPORARY CHUNKS
# ============================================================================

def delete_chunks(s):

    _stop_sequence(
        s,
        "full download finished"
    )

    with s.chunk_lock:
        s.chunks.clear()

    for seqdir in s.cache_dir.glob(
        "sequence_*"
    ):

        if seqdir.is_dir():

            shutil.rmtree(
                seqdir,
                ignore_errors=True
            )

    log(
        "STATUS",
        (
            f"{s.video_id}: "
            f"full download finished; "
            f"temporary playback chunks deleted"
        )
    )


# ============================================================================
# CHUNK WORKER
# ============================================================================

def chunk_worker(s):

    last_diag = 0.0

    while not s.chunk_stop.is_set():

        with s.lock:

            if s.full_done:
                break

        _register_sequence_segments(s)

        _finalize_sequence_process(s)

        target = s.requested_target

        covered = find_chunk_covering(
            s,
            target,
            include_incomplete=True
        )

        now = time.time()

        if (
            now - last_diag
            >= 1.0
        ):

            with s.chunk_lock:

                summary = ", ".join(
                    (
                        f"{c.index}:"
                        f"{c.start:.2f}-"
                        f"{c.end:.2f}:"
                        f"{c.status}"
                    )
                    for c in sorted(
                        s.chunks.values(),
                        key=lambda x: x.index
                    )
                )

            with s.sequence_lock:

                seq_alive = bool(
                    s.sequence_process
                    and s.sequence_process.poll()
                    is None
                )

                seqdir_now = (
                    str(s.sequence_dir)
                    if s.sequence_dir
                    else "none"
                )

            log(
                "DEBUG",
                (
                    f"{s.video_id}: "
                    f"target={target:.3f}s "
                    f"covered="
                    f"{covered.index if covered else None} "
                    f"current="
                    f"{s.current_playback_chunk} "
                    f"sequence_alive="
                    f"{seq_alive} "
                    f"seqdir="
                    f"{seqdir_now} "
                    f"chunks=[{summary}]"
                )
            )

            last_diag = now

        if covered is not None:

            with s.chunk_lock:
                s.current_playback_chunk = (
                    covered.index
                )

            if _sequence_process_alive(s):

                time.sleep(
                    POLL_INTERVAL
                )

                continue

        if not _sequence_process_alive(s):

            if covered is None:

                _start_sequence(
                    s,
                    target
                )

            else:

                with s.chunk_lock:

                    later = sorted(
                        [
                            c
                            for c in s.chunks.values()
                            if (
                                c.status == "complete"
                                and c.start
                                >= covered.end - 0.01
                            )
                        ],
                        key=lambda c: c.start
                    )

                if not later:

                    _start_sequence(
                        s,
                        covered.end
                    )

        time.sleep(0.12)


# ============================================================================
# SEEK
# ============================================================================

def request_seek(
    s,
    target
):
    target = max(
        0.0,
        min(
            float(target),
            max(
                0.0,
                s.duration - 0.02
            )
        )
    )

    with s.lock:

        if s.full_done:

            s.requested_target = target

            return (
                target,
                "full"
            )

    existing = find_chunk_covering(
        s,
        target,
        include_incomplete=True
    )

    active_sequence = (
        _sequence_process_alive(s)
    )

    if existing:

        s.requested_target = target

        with s.chunk_lock:

            s.current_playback_chunk = (
                existing.index
            )

        s.chunk_wake.set()

        return (
            target,
            existing.status
        )

    if active_sequence:

        _stop_sequence(
            s,
            "far seek"
        )

    s.requested_target = target

    with s.chunk_lock:
        s.current_playback_chunk = None

    s.chunk_wake.set()

    return (
        target,
        "new"
    )


# ============================================================================
# FULL DOWNLOAD
# ============================================================================

def full_download_worker(
    s,
    info
):
    outtmpl = str(
        s.cache_dir
        / "full.%(ext)s"
    )

    try:

        if s.progressive_fmt:

            fmt_expr = str(
                s.progressive_fmt.get(
                    "format_id"
                )
            )

        elif (
            s.video_fmt
            and s.audio_fmt
        ):

            fmt_expr = (
                f"{s.video_fmt.get('format_id')}"
                f"+"
                f"{s.audio_fmt.get('format_id')}"
            )

        else:

            fmt_expr = (
                "bestvideo[ext=mp4]"
                "+bestaudio[ext=m4a]"
                "/best[ext=mp4]"
                "/best"
            )

        def hook(d):

            st = d.get("status")

            if st == "downloading":

                total = (
                    d.get("total_bytes")
                    or d.get(
                        "total_bytes_estimate"
                    )
                )

                got = (
                    d.get("downloaded_bytes")
                    or 0
                )

                s.record_full(
                    got,
                    total,
                    d.get("total_bytes")
                    is not None
                )

            elif st == "finished":

                got = (
                    d.get("downloaded_bytes")
                    or 0
                )

                total = (
                    d.get("total_bytes")
                    or got
                )

                s.record_full(
                    got,
                    total,
                    d.get("total_bytes")
                    is not None
                )

        opts = yt_dlp_options(
            {
                "quiet": True,
                "no_warnings": True,
                "noplaylist": True,
                "outtmpl": outtmpl,
                "format": fmt_expr,
                "merge_output_format": "mp4",
                "progress_hooks": [hook],
                "retries": 5,
                "fragment_retries": 5,
            }
        )

        with yt_dlp.YoutubeDL(opts) as ydl:

            ydl.download(
                [s.original_url]
            )

        candidates = sorted(
            s.cache_dir.glob("full.*"),
            key=lambda p: p.stat().st_size,
            reverse=True
        )

        candidates = [
            p for p in candidates
            if p.suffix.lower()
            not in (
                ".part",
                ".ytdl",
            )
        ]

        if not candidates:

            raise RuntimeError(
                "yt-dlp finished but "
                "full output was not found"
            )

        final = candidates[0]

        with s.lock:

            s.full_file = final

            s.chunk_stop.set()

            s.full_done = True

            s.full_downloaded = (
                final.stat().st_size
            )

            s.full_total = (
                final.stat().st_size
            )

            s.full_exact = True

        s.chunk_stop.set()
        s.chunk_wake.set()

        with s.chunk_lock:
            active = None

        if active:
            kill_chunk_process(
                active,
                pause=False
            )

        log(
            "STATUS",
            (
                f"{s.video_id}: "
                f"FULL download complete "
                f"({fmt_size(final.stat().st_size)})"
            )
        )

        delete_chunks(s)

    except Exception as e:

        with s.lock:

            s.error_message = (
                f"Full download failed: {e}"
            )

            s.status = "error"

        log(
            "ERROR",
            (
                f"{s.video_id}: "
                f"full download failed: {e}"
            )
        )

        log(
            "ERROR",
            traceback.format_exc()
        )


# ============================================================================
# RESOLVE VIDEO
# ============================================================================

def resolve_and_start(url):

    log(
        "INFO",
        (
            "Resolving metadata for "
            f"submitted URL "
            f"({redact_url(url)})"
        )
    )

    info = extract_info(url)

    vid = sanitize_video_id(
        info.get("id")
    )

    title = (
        info.get("title")
        or "Untitled"
    )

    duration = float(
        info.get("duration")
        or 0
    )

    with SESSIONS_LOCK:

        old = SESSIONS.get(vid)

        if (
            old
            and old.status != "error"
        ):
            return old

        s = DownloadSession(
            vid,
            title,
            duration
        )

        s.original_url = url

        SESSIONS[vid] = s

    progressive, video, audio = (
        choose_streams(info)
    )

    s.progressive_fmt = progressive
    s.video_fmt = video
    s.audio_fmt = audio

    if progressive:

        s.mode = (
            "progressive + chunked"
        )

        s.mime = codec_string(
            progressive,
            progressive
        )

        log(
            "INFO",
            (
                f"{vid}: "
                f"progressive source "
                f"{progressive.get('format_id')} "
                f"selected"
            )
        )

    elif (
        video
        and audio
        and FFMPEG_AVAILABLE
    ):

        s.mode = (
            "adaptive + chunked"
        )

        s.mime = codec_string(
            video,
            audio
        )

        alang = (
            audio.get("language")
            or "unknown"
        )

        anote = (
            audio.get("format_note")
            or ""
        )

        log(
            "INFO",
            (
                f"{vid}: "
                f"adaptive sources "
                f"video={video.get('format_id')} "
                f"{video.get('height')}p "
                f"audio={audio.get('format_id')} "
                f"lang={alang} "
                f"note={anote!r} "
                f"abr={audio.get('abr')}"
            )
        )

    else:

        s.status = "error"

        s.error_message = (
            "Could not find an MP4 source "
            "suitable for local chunking, "
            "and FFmpeg is unavailable."
        )

        return s

    s.full_thread = threading.Thread(
        target=full_download_worker,
        args=(s, info),
        daemon=True
    )

    s.full_thread.start()

    s.chunk_worker = threading.Thread(
        target=chunk_worker,
        args=(s,),
        daemon=True
    )

    s.chunk_worker.start()

    s.status = "downloading"

    s.chunk_wake.set()

    return s


# ============================================================================
# HTTP HANDLER
# ============================================================================

class Handler(
    http.server.BaseHTTPRequestHandler
):

    server_version = (
        "LocalYouTubePlayer/13"
    )

    def log_message(
        self,
        fmt,
        *args
    ):
        pass

    # ------------------------------------------------------------------------
    # CORS
    # ------------------------------------------------------------------------

    def _cors_headers(self):

        self.send_header(
            "Access-Control-Allow-Origin",
            "*"
        )

        self.send_header(
            "Access-Control-Allow-Methods",
            "GET, POST, OPTIONS"
        )

        self.send_header(
            "Access-Control-Allow-Headers",
            "Content-Type, Range"
        )

        self.send_header(
            "Access-Control-Expose-Headers",
            (
                "Content-Length, "
                "Content-Range, "
                "Accept-Ranges"
            )
        )

    def do_OPTIONS(self):

        self.send_response(204)

        self._cors_headers()

        self.send_header(
            "Content-Length",
            "0"
        )

        self.end_headers()

    # ------------------------------------------------------------------------
    # JSON
    # ------------------------------------------------------------------------

    def _json(
        self,
        obj,
        code=200
    ):
        body = json.dumps(
            obj
        ).encode()

        self.send_response(code)

        self._cors_headers()

        self.send_header(
            "Content-Type",
            "application/json; charset=utf-8"
        )

        self.send_header(
            "Content-Length",
            str(len(body))
        )

        self.send_header(
            "Cache-Control",
            "no-store"
        )

        self.end_headers()

        self.wfile.write(body)

    def _error(
        self,
        msg,
        code=400
    ):
        self._json(
            {"error": msg},
            code
        )

    # ------------------------------------------------------------------------
    # BODY
    # ------------------------------------------------------------------------

    def _body(self):

        n = int(
            self.headers.get(
                "Content-Length",
                "0"
            )
        )

        if n > MAX_REQUEST_BODY:
            raise ValueError(
                "request body too large"
            )

        raw = self.rfile.read(n)

        return json.loads(
            raw.decode() or "{}"
        )

    # ------------------------------------------------------------------------
    # GET
    # ------------------------------------------------------------------------

    def do_GET(self):

        p = urllib.parse.urlsplit(
            self.path
        )

        try:

            if p.path == "/":

                return self._index()

            if p.path.startswith(
                "/api/status/"
            ):

                return self._status(
                    p.path.split("/")[-1]
                )

            if p.path.startswith(
                "/api/chunks/"
            ):

                return self._chunks(
                    p.path.split("/")[-1]
                )

            if p.path.startswith(
                "/api/full/"
            ):

                return self._full(
                    p.path.split("/")[-1]
                )

            if p.path.startswith(
                "/media/full/"
            ):

                return self._serve_named_file(
                    p.path[
                        len("/media/full/"):
                    ]
                )

            if p.path.startswith(
                "/media/chunk/"
            ):

                return self._serve_chunk(
                    p.path[
                        len("/media/chunk/"):
                    ]
                )

            self._error(
                "not found",
                404
            )

        except (
            BrokenPipeError,
            ConnectionResetError
        ):
            pass

        except Exception as e:

            log(
                "ERROR",
                (
                    f"GET {self.path}: "
                    f"{e}"
                )
            )

            try:

                self._error(
                    "internal server error",
                    500
                )

            except Exception:
                pass

    # ------------------------------------------------------------------------
    # POST
    # ------------------------------------------------------------------------

    def do_POST(self):

        p = urllib.parse.urlsplit(
            self.path
        ).path

        try:

            if p == "/api/load":

                return self._load()

            if p == "/api/seek":

                return self._seek()

            self._error(
                "not found",
                404
            )

        except ValueError as e:

            self._error(
                str(e),
                400
            )

        except Exception as e:

            log(
                "ERROR",
                (
                    f"POST {p}: "
                    f"{e}"
                )
            )

            self._error(
                "internal server error",
                500
            )

    # ------------------------------------------------------------------------
    # INDEX
    # ------------------------------------------------------------------------

    def _index(self):

        index_path = (
            BASE_DIR
            / "index.html"
        )

        if not index_path.exists():

            return self._error(
                "index.html is missing",
                500
            )

        body = index_path.read_bytes()

        self.send_response(200)

        self._cors_headers()

        self.send_header(
            "Content-Type",
            "text/html; charset=utf-8"
        )

        self.send_header(
            "Content-Length",
            str(len(body))
        )

        self.end_headers()

        self.wfile.write(body)

    # ------------------------------------------------------------------------
    # SESSION
    # ------------------------------------------------------------------------

    def _get_session(self, vid):

        vid = sanitize_video_id(
            vid
        )

        with SESSIONS_LOCK:

            s = SESSIONS.get(vid)

        if not s:

            raise ValueError(
                "unknown video id"
            )

        return s

    # ------------------------------------------------------------------------
    # LOAD
    # ------------------------------------------------------------------------

    def _load(self):

        if not YTDLP_AVAILABLE:

            return self._error(
                (
                    "yt-dlp is not installed. "
                    "Run: pip install -U yt-dlp"
                ),
                500
            )

        if not FFMPEG_AVAILABLE:

            return self._error(
                (
                    "FFmpeg is required for "
                    "chunked playback."
                ),
                500
            )

        data = self._body()

        url = (
            data.get("url")
            or ""
        ).strip()

        if (
            not url
            or not is_youtube_url(url)
        ):

            return self._error(
                "enter a valid YouTube URL",
                400
            )

        log(
            "INFO",
            (
                f"URL submitted: "
                f"{redact_url(url)}"
            )
        )

        try:

            s = resolve_and_start(
                url
            )

        except Exception as e:

            log(
                "ERROR",
                (
                    f"Metadata extraction "
                    f"failed: {e}"
                )
            )

            return self._error(
                str(e),
                500
            )

        self._json(
            {
                "video_id": s.video_id,
                "title": s.title,
                "duration": s.duration,
                "mode": s.mode,
                "status": s.status,
                "error": s.error_message,
                "mime": s.mime,
            }
        )

    # ------------------------------------------------------------------------
    # STATUS
    # ------------------------------------------------------------------------

    def _status(self, vid):

        s = self._get_session(
            vid
        )

        with s.lock:

            pct = s.progress()

            full_done = s.full_done

            data = {
                "video_id": s.video_id,
                "title": s.title,
                "duration": s.duration,
                "status": s.status,
                "mode": s.mode,
                "error": s.error_message,
                "full_downloaded": (
                    s.full_downloaded
                ),
                "full_total": (
                    s.full_total
                ),
                "full_percent": pct,
                "full_rate_bps": (
                    s.full_rate
                ),
                "full_done": full_done,
                "mime": s.mime,
                "requested_target": (
                    s.requested_target
                ),
                "current_playback_chunk": (
                    s.current_playback_chunk
                ),
            }

        with s.chunk_lock:

            chunks = [
                c.to_dict()
                for c in sorted(
                    s.chunks.values(),
                    key=lambda c: (
                        c.start,
                        c.index
                    )
                )
            ]

        data["chunks"] = chunks

        data["coverage_end"] = max(
            [
                c["end"]
                for c in chunks
                if c["status"]
                == "complete"
            ]
            + [0.0]
        )

        self._json(data)

    # ------------------------------------------------------------------------
    # CHUNKS
    # ------------------------------------------------------------------------

    def _chunks(self, vid):

        s = self._get_session(
            vid
        )

        with s.chunk_lock:

            chunks = [
                c.to_dict()
                for c in sorted(
                    s.chunks.values(),
                    key=lambda c: (
                        c.start,
                        c.index
                    )
                )
            ]

        self._json(
            {
                "mime": s.mime,
                "chunks": chunks,
            }
        )

    # ------------------------------------------------------------------------
    # FULL FILE
    # ------------------------------------------------------------------------

    def _full(self, vid):

        s = self._get_session(
            vid
        )

        final = s.final_file

        self._json(
            {
                "ready": bool(
                    final
                    and s.full_done
                ),
                "file": (
                    final.name
                    if final
                    else None
                ),
            }
        )

    # ------------------------------------------------------------------------
    # SEEK
    # ------------------------------------------------------------------------

    def _seek(self):

        d = self._body()

        s = self._get_session(
            d.get("video_id")
        )

        t = float(
            d.get("time", 0)
        )

        target, action = request_seek(
            s,
            t
        )

        log(
            "SEEK",
            (
                f"{s.video_id}: "
                f"seek -> {target:.2f}s "
                f"action={action} "
                f"(full download continues)"
            )
        )

        self._json(
            {
                "ok": True,
                "target": target,
                "action": action,
            }
        )

    # ------------------------------------------------------------------------
    # FULL MEDIA
    # ------------------------------------------------------------------------

    def _serve_named_file(
        self,
        suffix
    ):

        parts = (
            suffix
            .strip("/")
            .split("/")
        )

        if not parts:

            return self._error(
                "invalid media path",
                400
            )

        vid = sanitize_video_id(
            parts[0]
        )

        s = self._get_session(
            vid
        )

        path = s.final_file

        if (
            not path
            or not path.exists()
        ):

            return self._error(
                "full video is not ready",
                404
            )

        self._range_file(path)

    # ------------------------------------------------------------------------
    # CHUNK MEDIA
    # ------------------------------------------------------------------------

    def _serve_chunk(
        self,
        suffix
    ):

        parts = (
            suffix
            .strip("/")
            .split("/")
        )

        if len(parts) != 2:

            return self._error(
                "invalid chunk path",
                400
            )

        vid = sanitize_video_id(
            parts[0]
        )

        try:

            idx = int(
                parts[1]
            )

        except ValueError:

            return self._error(
                "invalid chunk index",
                400
            )

        s = self._get_session(
            vid
        )

        with s.chunk_lock:

            c = s.chunks.get(
                idx
            )

            path = (
                c.path
                if c
                else None
            )

            ready = bool(
                c
                and c.status == "complete"
                and path
                and path.exists()
            )

        if not ready:

            return self._error(
                "chunk is not ready",
                404
            )

        self._range_file(
            path
        )

    # ------------------------------------------------------------------------
    # RANGE FILE
    # ------------------------------------------------------------------------

    def _range_file(
        self,
        path
    ):

        total = path.stat().st_size

        if total <= 0:

            return self._error(
                "empty media file",
                404
            )

        rh = self.headers.get(
            "Range"
        )

        start = 0
        end = total - 1
        code = 200

        if rh:

            m = re.match(
                r"bytes=(\d*)-(\d*)",
                rh
            )

            if not m:

                return self._error(
                    "malformed Range",
                    416
                )

            a, b = m.groups()

            try:

                if a:

                    start = int(a)

                elif b:

                    start = max(
                        0,
                        total - int(b)
                    )

                if b:

                    end = int(b)

                else:

                    end = total - 1

            except ValueError:

                return self._error(
                    "malformed Range",
                    416
                )

            end = min(
                end,
                total - 1
            )

            if (
                start < 0
                or start >= total
                or start > end
            ):

                self.send_response(
                    416
                )

                self._cors_headers()

                self.send_header(
                    "Content-Range",
                    f"bytes */{total}"
                )

                self.send_header(
                    "Content-Length",
                    "0"
                )

                self.end_headers()

                return

            code = 206

        length = (
            end - start + 1
        )

        self.send_response(
            code
        )

        self._cors_headers()

        self.send_header(
            "Content-Type",
            "video/mp4"
        )

        self.send_header(
            "Accept-Ranges",
            "bytes"
        )

        self.send_header(
            "Content-Length",
            str(length)
        )

        self.send_header(
            "Cache-Control",
            "no-store"
        )

        if code == 206:

            self.send_header(
                "Content-Range",
                (
                    f"bytes {start}-"
                    f"{end}/{total}"
                )
            )

        self.end_headers()

        with open(
            path,
            "rb"
        ) as f:

            f.seek(start)

            left = length

            while left:

                b = f.read(
                    min(
                        256 * 1024,
                        left
                    )
                )

                if not b:
                    break

                self.wfile.write(b)

                left -= len(b)


# ============================================================================
# HTTP SERVER
# ============================================================================

class Server(
    socketserver.ThreadingMixIn,
    http.server.HTTPServer
):

    daemon_threads = True
    allow_reuse_address = True


# ============================================================================
# MAIN
# ============================================================================

def find_port():
    return PORT


def main():

    if not (
        BASE_DIR / "index.html"
    ).exists():

        print(
            "index.html is missing"
        )

        sys.exit(1)

    if not YTDLP_AVAILABLE:

        print(
            "WARNING: install yt-dlp "
            "with: pip install -U yt-dlp"
        )

    if not FFMPEG_AVAILABLE:

        print(
            "WARNING: FFmpeg is required "
            "for chunked playback"
        )

    cookie_path = Path(
        YTDLP_COOKIE_FILE
    )

    print("=" * 70)
    print(
        " Local YouTube-style "
        "Video Player v13"
    )
    print("=" * 70)

    print(
        f" Python version     : "
        f"{sys.version.split()[0]}"
    )

    print(
        f" yt-dlp available   : "
        f"{'yes' if YTDLP_AVAILABLE else 'NO'}"
    )

    print(
        f" ffmpeg available   : "
        f"{'yes' if FFMPEG_AVAILABLE else 'NO'}"
    )

    print(
        f" Host / Port        : "
        f"{HOST}:{PORT}"
    )

    print(
        f" Render PORT        : "
        f"{os.environ.get('PORT', 'not set')}"
    )

    print(
        f" Cookie file        : "
        f"{cookie_path}"
    )

    print(
        f" Cookies available  : "
        f"{'yes' if cookie_path.is_file() else 'NO'}"
    )

    print(
        f" Videos cache dir   : "
        f"{VIDEOS_DIR}"
    )

    print(
        " Architecture       : "
        "full download + continuous "
        "FFmpeg 3/5/10-second "
        "segment sequences"
    )

    print("=" * 70)

    httpd = Server(
        (HOST, find_port()),
        Handler
    )

    threading.Thread(
        target=httpd.serve_forever,
        daemon=True
    ).start()

    log(
        "STATUS",
        (
            f"Server started on "
            f"{HOST}:{PORT}"
        )
    )

    try:

        while True:
            time.sleep(1)

    except KeyboardInterrupt:

        log(
            "STATUS",
            "Stopping server"
        )

        httpd.shutdown()


if __name__ == "__main__":
    main()
