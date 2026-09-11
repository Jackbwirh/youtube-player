V13 FIXED HOTFIX — stop_reason / graceful shutdown correction

======================================================================
Local YouTube-style Video Player v13
======================================================================

V13 is the playback-chunk correctness fix for the v10/v11/v12 line.

CORE BEHAVIOR
-------------
- Paste a YouTube URL.
- Resolve H.264 video + preferred AAC audio with yt-dlp.
- Full-quality video downloads continuously in the background.
- Playback starts before the full download finishes.
- Playback uses separate MP4 chunk files and Media Source Extensions.
- New playback paths use 3s, then 5s, then 10s chunks forever.
- A seek inside an existing completed chunk reuses that chunk.
- A seek into a chunk being produced waits for that chunk instead of killing it.
- A far seek starts another playback sequence while preserving old completed chunks.
- Partial chunks are preserved.
- When the unrestricted full download finishes, temporary chunks are removed and
  playback switches to the completed full MP4.

V13 FIX
-------
Earlier versions used H.264 stream-copy segmentation. Arbitrary 3/5/10 second
cuts could land between source keyframes, producing physical MP4 segments that
were dramatically shorter than the timeline interval they were assigned to.
That caused visible skipped portions at splice boundaries.

V13 instead runs ONE continuous FFmpeg playback encode per sequence:
- H.264 is re-encoded continuously.
- A keyframe is forced at every requested 3/5/10 boundary.
- AAC is encoded continuously by the same FFmpeg process.
- The segment muxer writes separate fragmented MP4 files.
- Browser/MSE timeline positions remain authoritative source positions.

This avoids both major previous problems:
1. stream-copy/keyframe cuts dropping portions of video;
2. restarting AAC encoding for every chunk and causing audio splice artifacts.

ENCODER
-------
If FFmpeg exposes h264_amf, V13 uses the AMD hardware encoder for temporary
playback chunks. Otherwise it falls back to libx264 ultrafast CRF 23.
The full download remains the original unrestricted yt-dlp download path.

DIAGNOSTICS
-----------
The server/browser retain verbose v12 timing diagnostics, including:
- requested and physical chunk durations
- media stream start/duration information
- chunk registration and playback transitions
- MSE timestampOffset and buffered ranges
- seek/rebuild state
- full-download state

If a chunk's physical duration differs materially from its requested duration,
V13 logs a PHYSICAL DURATION MISMATCH warning.

INSTALL / RUN
-------------
Keep ffmpeg and yt-dlp available on PATH / in the same Python environment.
Run:
    python server.py

Then open the displayed localhost URL.
