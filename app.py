import os
import re
import uuid
import random
import requests
import subprocess
import tempfile
from flask import Flask, request, jsonify, send_file

app = Flask(__name__)

SWISH_URLS = os.environ.get("SWISH_URLS", "")
THROW_URL = os.environ.get("THROW_URL", "")
THROW_URLS = os.environ.get("THROW_URLS", "")
INTRO_URL = os.environ.get("INTRO_URL", "")
OUTRO_URL = os.environ.get("OUTRO_URL", "")
BEAT_URL = os.environ.get("BEAT_URL", "")
TEASER_BEAT_URL = os.environ.get("TEASER_BEAT_URL", "")
TEASER_MUSIC_URL = os.environ.get("TEASER_MUSIC_URL", "")
INTRO_SWISH_URL = os.environ.get("INTRO_SWISH_URL", "")
BUT_FIRST_SWISH_URL = os.environ.get("BUT_FIRST_SWISH_URL", "")
CLOSING_BEAT_URL = os.environ.get("CLOSING_BEAT_URL", "")
BACKGROUND_VIDEO_URL = os.environ.get("BACKGROUND_VIDEO_URL", "")
BACKGROUND_IMAGE_URL = os.environ.get("BACKGROUND_IMAGE_URL", "")
PEXELS_API_KEY = os.environ.get("PEXELS_API_KEY", "")

# MUSIC_URLS: comma-separated pool of background-music tracks for
# /make-video, one is picked at random per render (same pattern as
# SWISH_URLS/THROW_URLS below). Falls back to TEASER_MUSIC_URL if empty.
MUSIC_URLS = os.environ.get("MUSIC_URLS", "")

FFMPEG_TIMEOUT_SECONDS = 240

def download_file(url, dest_path, headers=None):
    r = requests.get(url, timeout=60, headers=headers or {})
    r.raise_for_status()
    with open(dest_path, "wb") as f:
        f.write(r.content)

def select_music_url():
    """Pick a random track from MUSIC_URLS (comma-separated pool), falling
    back to TEASER_MUSIC_URL if no pool is configured. Same pattern as the
    SWISH_URLS/THROW_URLS pool selection used elsewhere in this file."""
    pool = [u.strip() for u in MUSIC_URLS.split(',') if u.strip()]
    if pool:
        return random.choice(pool)
    return TEASER_MUSIC_URL or None

# NOTE: download_pexels_image() removed 2026-08-25. It attached a Pexels
# Authorization header to every image download, but image_urls now come
# from Google Drive share links (AI-generated illustrations), not Pexels
# -- that header was stale/wrong dead code left over from before the
# Pexels-to-AI-image-generation swap. All image downloads now just use
# download_file() directly, headerless.

def run_ffmpeg(cmd, error_label):
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=FFMPEG_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"{error_label}: timed out after {FFMPEG_TIMEOUT_SECONDS}s")
    if result.returncode != 0:
        raise RuntimeError(f"{error_label}: {result.stderr}")
    return result

def stitch_audio(file_paths, output_path):
    """Stitch with per-clip loudnorm — used for main story block."""
    normalized_paths = []
    tmpdir = os.path.dirname(output_path)
    for idx, p in enumerate(file_paths):
        norm_path = os.path.join(tmpdir, f"norm_{idx}_{uuid.uuid4().hex[:6]}.mp3")
        norm_cmd = [
            "ffmpeg", "-y",
            "-i", p,
            "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
            "-ar", "44100",
            "-ac", "2",
            "-acodec", "libmp3lame",
            "-q:a", "2",
            norm_path
        ]
        run_ffmpeg(norm_cmd, f"FFmpeg normalize error on file {idx}")
        normalized_paths.append(norm_path)

    list_path = output_path + ".txt"
    with open(list_path, "w") as f:
        for p in normalized_paths:
            f.write(f"file '{p}'\n")
    cmd = [
        "ffmpeg", "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", list_path,
        "-acodec", "libmp3lame",
        "-q:a", "2",
        output_path
    ]
    try:
        run_ffmpeg(cmd, "FFmpeg error")
    finally:
        os.unlink(list_path)
        for p in normalized_paths:
            os.unlink(p)

def stitch_audio_raw(file_paths, output_path):
    """Stitch without per-clip normalisation — used for teaser block so the
    whole assembled block can be normalised as one unit before music mixing,
    preventing volume fluctuation between short and long clips."""
    tmpdir = os.path.dirname(output_path)
    list_path = output_path + ".txt"
    with open(list_path, "w") as f:
        for p in file_paths:
            f.write(f"file '{p}'\n")
    cmd = [
        "ffmpeg", "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", list_path,
        "-acodec", "libmp3lame",
        "-q:a", "2",
        output_path
    ]
    try:
        run_ffmpeg(cmd, "FFmpeg raw stitch error")
    finally:
        os.unlink(list_path)

def normalise_audio(input_path, output_path):
    """Apply loudnorm to a single file as one unit."""
    cmd = [
        "ffmpeg", "-y",
        "-i", input_path,
        "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
        "-ar", "44100",
        "-ac", "2",
        "-acodec", "libmp3lame",
        "-q:a", "2",
        output_path
    ]
    run_ffmpeg(cmd, "FFmpeg normalise error")

def mix_beat_under_audio(voice_path, beat_path, output_path, volume="0.15"):
    cmd = [
        "ffmpeg", "-y",
        "-i", voice_path,
        "-stream_loop", "-1",
        "-i", beat_path,
        "-filter_complex",
        f"[1:a]volume={volume}[beat];[0:a][beat]amix=inputs=2:duration=first:dropout_transition=2[out]",
        "-map", "[out]",
        "-acodec", "libmp3lame",
        "-q:a", "2",
        output_path
    ]
    run_ffmpeg(cmd, "FFmpeg beat-mix error")

def mix_beat_under_audio_with_tail(voice_path, beat_path, output_path, volume="0.15", tail_seconds=0.5, fade_duration=0.5, safety_buffer=0.15):
    """Mix beat under voice, extend beat by tail_seconds past voice end, then fade out.

    FIX (pun-tail truncation bug): this used to compute fade_start and the
    hard -t output cap directly from get_audio_duration(voice_path). That
    duration used to come from ffprobe's container-level metadata, which is
    unreliable on the headerless VBR MP3s ElevenLabs streams back — it tends
    to read the file as slightly SHORTER than the actual decoded audio. That
    meant the fade-out could start (and the -t cap could cut) before the
    voice had actually finished speaking, silently swallowing the last word
    or two of a segment. This was the real cause of the "...sorry" missing
    "about that one" bug — the text and TTS were always fine, the render was
    trimming the tail early.

    Two changes fix this:
    1. get_audio_duration() below now decodes the file (ffmpeg -f null -)
       instead of trusting container metadata, which is accurate regardless
       of missing VBR headers.
    2. A small safety_buffer is added on top of that, so even a residual
       few-hundred-ms measurement error can't clip real speech.
    """
    voice_duration = get_audio_duration(voice_path)
    total_duration = voice_duration + tail_seconds + safety_buffer
    fade_start = total_duration - fade_duration

    cmd = [
        "ffmpeg", "-y",
        "-i", voice_path,
        "-stream_loop", "-1",
        "-i", beat_path,
        "-filter_complex",
        (
            f"[0:a]apad=pad_dur={tail_seconds + safety_buffer}[voice_padded];"
            f"[1:a]volume={volume}[beat_vol];"
            f"[voice_padded][beat_vol]amix=inputs=2:duration=first:dropout_transition=0[mixed];"
            f"[mixed]afade=t=out:st={fade_start:.3f}:d={fade_duration:.3f}[out]"
        ),
        "-map", "[out]",
        "-t", f"{total_duration:.3f}",
        "-acodec", "libmp3lame",
        "-q:a", "2",
        output_path
    ]
    run_ffmpeg(cmd, "FFmpeg beat-mix-with-tail error")

def apply_fade_out(input_path, output_path, fade_duration=1.5):
    """Apply fade out to end of audio. Default 1.5s for smooth transition
    before the throw sting fires after the last story segment."""
    duration = get_audio_duration(input_path)
    fade_start = max(0, duration - fade_duration)
    cmd = [
        "ffmpeg", "-y",
        "-i", input_path,
        "-af", f"afade=t=out:st={fade_start:.3f}:d={fade_duration:.3f}",
        "-acodec", "libmp3lame",
        "-q:a", "2",
        output_path
    ]
    run_ffmpeg(cmd, "FFmpeg fade-out error")

def get_audio_duration(audio_path):
    """Get accurate audio duration by decoding the whole file, rather than
    trusting container-level metadata (ffprobe's format=duration).

    WHY: MP3 files without a proper Xing/VBRI header — which describes
    ElevenLabs' streamed TTS output — cause ffprobe to estimate duration
    from bitrate math instead of reading real frame data, and that estimate
    is often a bit SHORT of the true length. Anything downstream that trims
    or fades based on that number (see mix_beat_under_audio_with_tail above)
    ends up cutting into real audio. Decoding the file with `ffmpeg -f null -`
    and reading the final `time=` progress line reflects the actual decoded
    duration and is reliable regardless of missing headers.
    """
    cmd = ["ffmpeg", "-i", audio_path, "-f", "null", "-"]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    matches = re.findall(r"time=(\d+):(\d+):(\d+\.\d+)", result.stderr)
    if matches:
        h, m, s = matches[-1]
        return int(h) * 3600 + int(m) * 60 + float(s)
    # Fall back to container metadata only if decode-based parsing fails
    return _get_audio_duration_ffprobe(audio_path)

def _get_audio_duration_ffprobe(audio_path):
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        audio_path
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe error: {result.stderr}")
    return float(result.stdout.strip())

def build_video_from_video_bg(video_path, audio_path, output_path):
    cmd = [
        "ffmpeg", "-y",
        "-stream_loop", "-1",
        "-i", video_path,
        "-i", audio_path,
        "-map", "0:v",
        "-map", "1:a",
        "-vf", "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,setsar=1",
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-crf", "28",
        "-c:a", "aac",
        "-b:a", "192k",
        "-shortest",
        "-movflags", "+faststart",
        "-pix_fmt", "yuv420p",
        output_path
    ]
    run_ffmpeg(cmd, "FFmpeg video-bg error")

def build_video_from_image_bg(image_path, audio_path, output_path):
    cmd = [
        "ffmpeg", "-y",
        "-loop", "1",
        "-i", image_path,
        "-i", audio_path,
        "-vf", "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,setsar=1",
        "-c:v", "libx264",
        "-tune", "stillimage",
        "-preset", "ultrafast",
        "-crf", "28",
        "-c:a", "aac",
        "-b:a", "192k",
        "-pix_fmt", "yuv420p",
        "-shortest",
        "-movflags", "+faststart",
        output_path
    ]
    run_ffmpeg(cmd, "FFmpeg image-bg error")

def build_single_image_segment(image_path, duration, output_path):
    """Render one image as a short silent video segment of the given
    duration. Processes ONE image at a time so peak memory only ever
    holds a single decoded image stream — see build_video_from_multi_image_bg
    below for why this matters."""
    cmd = [
        "ffmpeg", "-y",
        "-loop", "1",
        "-t", f"{duration:.3f}",
        "-i", image_path,
        "-vf", "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,setsar=1,fps=30",
        "-c:v", "libx264",
        "-tune", "stillimage",
        "-preset", "ultrafast",
        "-crf", "28",
        "-pix_fmt", "yuv420p",
        output_path
    ]
    run_ffmpeg(cmd, "FFmpeg single-image segment error")

def build_video_from_multi_image_bg(image_paths, audio_path, output_path):
    """Build a slideshow video from multiple images, hard-cutting between
    them rather than crossfading.

    FIX (Render OOM crash, 2026-08-25): the previous version used ffmpeg's
    xfade filter to crossfade between images, which requires decoding and
    holding multiple full-resolution 1080x1920 video streams in memory
    simultaneously during the blend. On Render's free tier (512MB RAM)
    this reliably killed the worker mid-request — confirmed via repeated
    ungraceful worker restarts in Render's logs, and NOT fixed by smaller
    source images, since every image gets scaled up to 1080x1920 before
    the crossfade regardless of its original size. Source resolution was
    never the actual bottleneck; the simultaneous multi-stream blend was.

    This version renders each image as its own small silent video segment
    ONE AT A TIME (build_single_image_segment), so peak memory only ever
    holds a single image stream, then concatenates the segments with
    ffmpeg's lightweight concat demuxer (a cheap container-level join, no
    re-encoding) before muxing in the audio track. Trade-off: hard cuts
    between images instead of smooth crossfades.
    """
    n = len(image_paths)
    if n < 1:
        raise RuntimeError("At least one image is required for a slideshow")

    tmpdir = os.path.dirname(output_path)
    total_duration = get_audio_duration(audio_path)
    per_image_duration = total_duration / n

    segment_paths = []
    concat_list_path = None
    silent_video_path = None
    try:
        for i, img_path in enumerate(image_paths):
            seg_path = os.path.join(tmpdir, f"seg_{i}_{uuid.uuid4().hex[:6]}.mp4")
            build_single_image_segment(img_path, per_image_duration, seg_path)
            segment_paths.append(seg_path)

        concat_list_path = os.path.join(tmpdir, f"concat_{uuid.uuid4().hex[:6]}.txt")
        with open(concat_list_path, "w") as f:
            for p in segment_paths:
                f.write(f"file '{p}'\n")

        silent_video_path = os.path.join(tmpdir, f"silent_{uuid.uuid4().hex[:6]}.mp4")
        concat_cmd = [
            "ffmpeg", "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", concat_list_path,
            "-c", "copy",
            silent_video_path
        ]
        run_ffmpeg(concat_cmd, "FFmpeg segment concat error")

        mux_cmd = [
            "ffmpeg", "-y",
            "-i", silent_video_path,
            "-i", audio_path,
            "-map", "0:v",
            "-map", "1:a",
            "-c:v", "copy",
            "-c:a", "aac",
            "-b:a", "192k",
            "-shortest",
            "-movflags", "+faststart",
            output_path
        ]
        run_ffmpeg(mux_cmd, "FFmpeg audio-mux error")

    finally:
        for p in segment_paths:
            if os.path.exists(p):
                os.unlink(p)
        if concat_list_path and os.path.exists(concat_list_path):
            os.unlink(concat_list_path)
        if silent_video_path and os.path.exists(silent_video_path):
            os.unlink(silent_video_path)

def build_video_from_segments(segments, output_path, music_url=None):
    """Build a video where each image is shown for exactly the duration of
    its OWN corresponding audio segment, rather than splitting total audio
    duration evenly across images. This is what makes image changes land
    exactly on topic/headline changes instead of an approximate even split.

    `segments` is a list of {"audio_path": ..., "image_path": ...} dicts,
    already downloaded to disk, in the order they should play. Each
    segment's real duration is measured directly (get_audio_duration) —
    no assumption of equal length between segments.

    Reuses the same building blocks as build_video_from_multi_image_bg
    (one-image-at-a-time rendering + lightweight concat demuxer) to avoid
    the memory-heavy xfade crossfade pattern that caused the 2026-08-25
    Render OOM crash.
    """
    if not segments:
        raise RuntimeError("At least one segment is required")

    tmpdir = os.path.dirname(output_path)

    # 1. Concatenate all segment audio into one narration track. Raw concat
    #    (no per-clip normalisation) then normalise as one unit, same
    #    reasoning as stitch_audio_raw's docstring: prevents volume jumps
    #    between segments of different length/loudness.
    audio_paths = [s["audio_path"] for s in segments]
    raw_narration_path = os.path.join(tmpdir, f"narration_raw_{uuid.uuid4().hex[:6]}.mp3")
    stitch_audio_raw(audio_paths, raw_narration_path)

    narration_path = os.path.join(tmpdir, f"narration_norm_{uuid.uuid4().hex[:6]}.mp3")
    normalise_audio(raw_narration_path, narration_path)

    final_audio_path = narration_path
    if music_url:
        try:
            music_path = os.path.join(tmpdir, f"seg_music_{uuid.uuid4().hex[:6]}.mp3")
            download_file(music_url, music_path)
            mixed_path = os.path.join(tmpdir, f"narration_mixed_{uuid.uuid4().hex[:6]}.mp3")
            mix_beat_under_audio(narration_path, music_path, mixed_path, volume="0.12")
            final_audio_path = mixed_path
        except Exception as e:
            print(f"Music mixing failed, continuing without music: {e}")

    # 2. Build one silent video segment per image, each timed to its own
    #    audio segment's REAL measured duration.
    segment_video_paths = []
    concat_list_path = None
    silent_video_path = None
    try:
        for i, seg in enumerate(segments):
            seg_duration = get_audio_duration(seg["audio_path"])
            seg_video_path = os.path.join(tmpdir, f"segvid_{i}_{uuid.uuid4().hex[:6]}.mp4")
            build_single_image_segment(seg["image_path"], seg_duration, seg_video_path)
            segment_video_paths.append(seg_video_path)

        concat_list_path = os.path.join(tmpdir, f"segconcat_{uuid.uuid4().hex[:6]}.txt")
        with open(concat_list_path, "w") as f:
            for p in segment_video_paths:
                f.write(f"file '{p}'\n")

        silent_video_path = os.path.join(tmpdir, f"segsilent_{uuid.uuid4().hex[:6]}.mp4")
        concat_cmd = [
            "ffmpeg", "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", concat_list_path,
            "-c", "copy",
            silent_video_path
        ]
        run_ffmpeg(concat_cmd, "FFmpeg segment concat error")

        mux_cmd = [
            "ffmpeg", "-y",
            "-i", silent_video_path,
            "-i", final_audio_path,
            "-map", "0:v",
            "-map", "1:a",
            "-c:v", "copy",
            "-c:a", "aac",
            "-b:a", "192k",
            "-shortest",
            "-movflags", "+faststart",
            output_path
        ]
        run_ffmpeg(mux_cmd, "FFmpeg audio-mux error")

    finally:
        for p in segment_video_paths:
            if os.path.exists(p):
                os.unlink(p)
        if concat_list_path and os.path.exists(concat_list_path):
            os.unlink(concat_list_path)
        if silent_video_path and os.path.exists(silent_video_path):
            os.unlink(silent_video_path)

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})

@app.route("/stitch", methods=["POST"])
def stitch():
    raw_body = request.get_data(as_text=True)
    print(f"Raw body received: {raw_body[:500]}")
    print(f"Content-Type: {request.content_type}")

    data = request.get_json(force=True, silent=True)
    if data is None:
        return jsonify({
            "error": "Could not parse JSON body",
            "raw_body": raw_body[:200],
            "content_type": request.content_type
        }), 400

    if "stories" not in data:
        return jsonify({
            "error": "Missing 'stories' key in JSON",
            "keys_received": list(data.keys())
        }), 400

    stories = data["stories"]
    if not isinstance(stories, list) or len(stories) < 1:
        return jsonify({"error": "Expected at least 1 story URL"}), 400

    teaser_segment_count = int(data.get("teaserSegmentCount", 1))

    intro_url = data.get("intro_url") or INTRO_URL
    outro_url = data.get("outro_url") or OUTRO_URL
    throw_url = data.get("throw_url")
    beat_url = data.get("beat_url") or BEAT_URL
    teaser_music_url = data.get("teaser_music_url") or TEASER_MUSIC_URL
    closing_beat_url = data.get("closing_beat_url") or CLOSING_BEAT_URL

    # intro_swish — new audio, plays after leadIn and between each tease line,
    # and once more before "But first,". Nothing plays after "But first,".
    intro_swish_url = data.get("intro_swish_url") or INTRO_SWISH_URL

    swish_url = data.get("swish_url")
    if not swish_url:
        swish_pool = [u.strip() for u in SWISH_URLS.split(',') if u.strip()]
        if swish_pool:
            swish_url = random.choice(swish_pool)

    if not throw_url:
        throw_pool = [u.strip() for u in THROW_URLS.split(',') if u.strip()]
        if throw_pool:
            throw_url = random.choice(throw_pool)
        else:
            throw_url = THROW_URL

    if not all([intro_url, outro_url, throw_url]):
        return jsonify({"error": "Missing intro, outro, or throw URL"}), 400

    job_id = str(uuid.uuid4())[:8]
    tmpdir = tempfile.mkdtemp()
    output_path = os.path.join(tmpdir, f"episode_{job_id}.mp3")

    try:
        intro_path = os.path.join(tmpdir, "intro.mp3")
        outro_path = os.path.join(tmpdir, "outro.mp3")
        throw_path = os.path.join(tmpdir, "throw.mp3")
        download_file(intro_url, intro_path)
        download_file(outro_url, outro_path)
        download_file(throw_url, throw_path)

        swish_path = None
        if swish_url:
            swish_path = os.path.join(tmpdir, "swish.mp3")
            download_file(swish_url, swish_path)

        intro_swish_path = None
        if intro_swish_url:
            intro_swish_path = os.path.join(tmpdir, "intro_swish.mp3")
            download_file(intro_swish_url, intro_swish_path)

        story_paths = []
        for i, url in enumerate(stories):
            p = os.path.join(tmpdir, f"story_{i+1}.mp3")
            download_file(url, p)
            story_paths.append(p)

        teaser_paths = story_paths[:teaser_segment_count]
        remaining_paths = story_paths[teaser_segment_count:]
        closing_path = remaining_paths[-1] if len(remaining_paths) >= 1 else None
        real_story_paths = remaining_paths[:-1] if len(remaining_paths) > 1 else []

        # ── BUILD TEASER BLOCK ────────────────────────────────────────────────
        # Sequence:
        # [leadIn] [swish] [tease1] [swish] [tease2] [swish] [tease3] [swish] [tease4] [swish] [But first,]
        #
        # Swish plays after every teaser segment including after tease4
        # and before "But first,". Nothing plays after "But first,".
        teaser_sequence = []
        if teaser_paths:
            last_teaser_idx = len(teaser_paths) - 1
            for i, tp in enumerate(teaser_paths):
                teaser_sequence.append(tp)
                if i < last_teaser_idx:
                    # Swish after each tease line (not after "But first,")
                    if intro_swish_path:
                        teaser_sequence.append(intro_swish_path)
                # No swish after "But first," — leads directly into story 1
        else:
            teaser_sequence = list(teaser_paths)

        # Insert swish before "But first," (i.e. before the last segment)
        # by rebuilding with swish between tease4 and But first
        if teaser_paths and intro_swish_path and len(teaser_paths) > 1:
            rebuilt = []
            last_idx = len(teaser_paths) - 1
            for i, tp in enumerate(teaser_paths):
                rebuilt.append(tp)
                if i == last_idx - 1:
                    # After tease4, add swish before "But first,"
                    rebuilt.append(intro_swish_path)
                elif i < last_idx - 1:
                    # Between other tease lines
                    rebuilt.append(intro_swish_path)
                # i == last_idx is "But first," — nothing after it
            teaser_sequence = rebuilt

        final_teaser_path = None
        if teaser_sequence:
            raw_teaser_path = os.path.join(tmpdir, "teaser_raw.mp3")
            stitch_audio_raw(teaser_sequence, raw_teaser_path)

            norm_teaser_path = os.path.join(tmpdir, "teaser_norm.mp3")
            normalise_audio(raw_teaser_path, norm_teaser_path)

            final_teaser_path = norm_teaser_path
            if teaser_music_url:
                try:
                    teaser_music_path = os.path.join(tmpdir, "teaser_music.mp3")
                    download_file(teaser_music_url, teaser_music_path)
                    mixed_teaser_path = os.path.join(tmpdir, "teaser_mixed.mp3")
                    mix_beat_under_audio(norm_teaser_path, teaser_music_path, mixed_teaser_path, volume="0.12")
                    final_teaser_path = mixed_teaser_path
                    print("Teaser music mixed successfully")
                except Exception as e:
                    print(f"Teaser music mixing failed, using dry teaser: {e}")

        # ── BUILD CLOSING SEGMENT WITH BEAT AND TAIL ─────────────────────────
        final_closing_path = closing_path
        if closing_path and closing_beat_url:
            try:
                closing_beat_path = os.path.join(tmpdir, "closing_beat.mp3")
                download_file(closing_beat_url, closing_beat_path)
                mixed_closing_path = os.path.join(tmpdir, "closing_mixed.mp3")
                mix_beat_under_audio_with_tail(
                    closing_path,
                    closing_beat_path,
                    mixed_closing_path,
                    volume="0.10",
                    tail_seconds=0.5,
                    fade_duration=0.5
                )
                final_closing_path = mixed_closing_path
                print("Closing beat mixed with tail successfully")
            except Exception as e:
                print(f"Closing beat mixing failed, using dry closing: {e}")

        # ── BUILD MAIN STORIES BLOCK ──────────────────────────────────────────
        if real_story_paths:
            last_story_path = real_story_paths[-1]
            faded_last_story_path = os.path.join(tmpdir, "last_story_faded.mp3")
            try:
                apply_fade_out(last_story_path, faded_last_story_path, fade_duration=1.5)
                real_story_paths[-1] = faded_last_story_path
                print("Fade out applied to last story segment")
            except Exception as e:
                print(f"Fade out failed, using original: {e}")

        real_sequence = []
        real_last_index = len(real_story_paths) - 1
        for i, sp in enumerate(real_story_paths):
            real_sequence.append(sp)
            if swish_path and real_last_index > 0 and i < real_last_index:
                real_sequence.append(swish_path)

        final_stories_block = None
        if real_sequence:
            stories_block_path = os.path.join(tmpdir, "stories_block.mp3")
            stitch_audio(real_sequence, stories_block_path)

            final_stories_block = stories_block_path
            if beat_url:
                try:
                    beat_path = os.path.join(tmpdir, "beat.mp3")
                    download_file(beat_url, beat_path)
                    mixed_path = os.path.join(tmpdir, "stories_block_mixed.mp3")
                    mix_beat_under_audio(stories_block_path, beat_path, mixed_path)
                    final_stories_block = mixed_path
                except Exception as e:
                    print(f"Beat mixing failed, continuing without beat: {e}")

        # ── ASSEMBLE FINAL EPISODE ────────────────────────────────────────────
        final_sequence = [intro_path]
        if final_teaser_path:
            final_sequence.append(final_teaser_path)
        if final_stories_block:
            final_sequence.append(final_stories_block)
        if final_closing_path:
            final_sequence.append(throw_path)
            final_sequence.append(final_closing_path)
        final_sequence.append(outro_path)

        stitch_audio(final_sequence, output_path)

        return send_file(
            output_path,
            mimetype="audio/mpeg",
            as_attachment=True,
            download_name=f"happy_day_news_{job_id}.mp3"
        )

    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/make-video", methods=["POST"])
def make_video():
    raw_body = request.get_data(as_text=True)
    print(f"Raw body received (make-video): {raw_body[:500]}")

    data = request.get_json(force=True, silent=True)
    if data is None:
        return jsonify({
            "error": "Could not parse JSON body",
            "raw_body": raw_body[:200]
        }), 400

    # ── SEGMENTS MODE (exact per-headline image/audio sync) ──────────────
    # Added 2026-08-25. If the request includes "segments" (a list of
    # {"audio_url": ..., "image_url": ...} pairs, one per headline, in
    # order), each image is shown for exactly the duration of its own
    # audio clip rather than an even time-split across images -- image
    # changes land exactly on topic changes. This is a separate path from
    # the original audio_url + image_urls mode below, which is left
    # unchanged for backward compatibility.
    segments_data = data.get("segments")
    if isinstance(segments_data, list) and len(segments_data) > 0:
        job_id = str(uuid.uuid4())[:8]
        tmpdir = tempfile.mkdtemp()
        output_path = os.path.join(tmpdir, f"episode_video_{job_id}.mp4")
        try:
            downloaded_segments = []
            for i, seg in enumerate(segments_data):
                seg_audio_url = seg.get("audio_url")
                seg_image_url = seg.get("image_url")
                if not seg_audio_url or not seg_image_url:
                    return jsonify({"error": f"segments[{i}] missing audio_url or image_url"}), 400
                a_path = os.path.join(tmpdir, f"seg_audio_{i}.mp3")
                i_path = os.path.join(tmpdir, f"seg_image_{i}.jpg")
                print(f"Downloading segment {i}: audio + image")
                download_file(seg_audio_url, a_path)
                download_file(seg_image_url, i_path)
                downloaded_segments.append({"audio_path": a_path, "image_path": i_path})

            music_url = data.get("music_url") or select_music_url()
            print(f"Building synced-segments video ({len(downloaded_segments)} segments)")
            build_video_from_segments(downloaded_segments, output_path, music_url=music_url)
            print("Synced-segments video built successfully")

            return send_file(
                output_path,
                mimetype="video/mp4",
                as_attachment=True,
                download_name=f"happy_day_news_video_{job_id}.mp4"
            )
        except Exception as e:
            print(f"make-video (segments mode) error: {e}")
            return jsonify({"error": str(e)}), 500

    # ── ORIGINAL MODE (single audio_url + image_urls, even time-split) ───
    audio_url = data.get("audio_url")
    if not audio_url:
        return jsonify({"error": "Missing 'audio_url' in JSON"}), 400

    image_urls = data.get("image_urls")
    has_explicit_image_urls = isinstance(image_urls, list) and len(image_urls) > 0

    if has_explicit_image_urls:
        video_url = None
    else:
        video_url = data.get("video_url") or BACKGROUND_VIDEO_URL

    image_url = data.get("image_url") or BACKGROUND_IMAGE_URL

    if not video_url and not has_explicit_image_urls and not image_url:
        return jsonify({"error": "No background video, image, or image list URL configured"}), 400

    job_id = str(uuid.uuid4())[:8]
    tmpdir = tempfile.mkdtemp()
    audio_path = os.path.join(tmpdir, "audio.mp3")
    output_path = os.path.join(tmpdir, f"episode_video_{job_id}.mp4")

    used_fallback = False

    try:
        print(f"Downloading audio from: {audio_url}")
        download_file(audio_url, audio_path)
        print(f"Audio downloaded successfully")

        music_url = data.get("music_url") or select_music_url()
        if music_url:
            try:
                print(f"Mixing background music: {music_url}")
                music_path = os.path.join(tmpdir, "music.mp3")
                download_file(music_url, music_path)
                mixed_audio_path = os.path.join(tmpdir, "audio_mixed.mp3")
                mix_beat_under_audio(audio_path, music_path, mixed_audio_path, volume="0.12")
                audio_path = mixed_audio_path
                print("Background music mixed successfully")
            except Exception as e:
                print(f"Music mixing failed, continuing without music: {e}")

        video_succeeded = False

        if video_url:
            try:
                print(f"Trying video background: {video_url}")
                video_path = os.path.join(tmpdir, "background.mp4")
                download_file(video_url, video_path)
                build_video_from_video_bg(video_path, audio_path, output_path)
                video_succeeded = True
                print(f"Video background succeeded")
            except Exception as e:
                print(f"Video background failed, falling back to image: {e}")
                used_fallback = True

        if not video_succeeded:
            if has_explicit_image_urls:
                print(f"Downloading {len(image_urls)} images")
                image_paths = []
                for i, url in enumerate(image_urls):
                    img_path = os.path.join(tmpdir, f"slide_{i}.jpg")
                    download_file(url, img_path)
                    print(f"Image {i+1} downloaded")
                    image_paths.append(img_path)
                print(f"Building multi-image slideshow")
                build_video_from_multi_image_bg(image_paths, audio_path, output_path)
                print(f"Slideshow built successfully")
            elif image_url:
                print(f"Downloading single background image")
                image_path = os.path.join(tmpdir, "background.jpg")
                download_file(image_url, image_path)
                build_video_from_image_bg(image_path, audio_path, output_path)
            else:
                return jsonify({"error": "Video background failed and no fallback image(s) configured"}), 500

        return send_file(
            output_path,
            mimetype="video/mp4",
            as_attachment=True,
            download_name=f"happy_day_news_video_{job_id}.mp4"
        )

    except Exception as e:
        print(f"make-video error: {e}")
        return jsonify({"error": str(e), "used_fallback": used_fallback if 'used_fallback' in locals() else False}), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
