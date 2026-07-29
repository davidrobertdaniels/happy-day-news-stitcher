import os
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

FFMPEG_TIMEOUT_SECONDS = 240

def download_file(url, dest_path, headers=None):
    r = requests.get(url, timeout=60, headers=headers or {})
    r.raise_for_status()
    with open(dest_path, "wb") as f:
        f.write(r.content)

def download_pexels_image(url, dest_path):
    headers = {"Authorization": PEXELS_API_KEY} if PEXELS_API_KEY else {}
    download_file(url, dest_path, headers=headers)

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

def mix_beat_under_audio_with_tail(voice_path, beat_path, output_path, volume="0.15", tail_seconds=0.5, fade_duration=0.5):
    """Mix beat under voice, extend beat by tail_seconds past voice end, then fade out."""
    voice_duration = get_audio_duration(voice_path)
    total_duration = voice_duration + tail_seconds
    fade_start = total_duration - fade_duration

    cmd = [
        "ffmpeg", "-y",
        "-i", voice_path,
        "-stream_loop", "-1",
        "-i", beat_path,
        "-filter_complex",
        (
            f"[0:a]apad=pad_dur={tail_seconds}[voice_padded];"
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

def build_video_from_multi_image_bg(image_paths, audio_path, output_path, transition_duration=0.75):
    n = len(image_paths)
    if n < 1:
        raise RuntimeError("At least one image is required for a slideshow")
    if n == 1:
        build_video_from_image_bg(image_paths[0], audio_path, output_path)
        return

    total_duration = get_audio_duration(audio_path)
    per_image_share = total_duration / n
    segment_duration = per_image_share + transition_duration

    inputs = []
    for p in image_paths:
        inputs += ["-loop", "1", "-t", f"{segment_duration:.3f}", "-i", p]

    filter_parts = []
    for i in range(n):
        filter_parts.append(
            f"[{i}:v]scale=1080:1920:force_original_aspect_ratio=increase,"
            f"crop=1080:1920,setsar=1,fps=30[v{i}]"
        )

    current_label = "v0"
    cumulative_offset = per_image_share - transition_duration
    if cumulative_offset < 0:
        cumulative_offset = 0
    for i in range(1, n):
        next_label = f"x{i}" if i < n - 1 else "vout"
        filter_parts.append(
            f"[{current_label}][v{i}]xfade=transition=fade:"
            f"duration={transition_duration:.3f}:offset={cumulative_offset:.3f}[{next_label}]"
        )
        current_label = next_label
        cumulative_offset += per_image_share - transition_duration

    filter_complex = ";".join(filter_parts)

    cmd = [
        "ffmpeg", "-y",
        *inputs,
        "-i", audio_path,
        "-filter_complex", filter_complex,
        "-map", f"[{current_label}]",
        "-map", f"{n}:a",
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-crf", "28",
        "-c:a", "aac",
        "-b:a", "192k",
        "-pix_fmt", "yuv420p",
        "-t", f"{total_duration:.3f}",
        "-movflags", "+faststart",
        output_path
    ]
    run_ffmpeg(cmd, "FFmpeg multi-image slideshow error")

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

    intro_swish_url = data.get("intro_swish_url") or INTRO_SWISH_URL
    but_first_swish_url = data.get("but_first_swish_url") or BUT_FIRST_SWISH_URL

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

        but_first_swish_path = None
        if but_first_swish_url:
            but_first_swish_path = os.path.join(tmpdir, "but_first_swish.mp3")
            download_file(but_first_swish_url, but_first_swish_path)
        elif intro_swish_path:
            but_first_swish_path = intro_swish_path

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
        teaser_sequence = []
        if teaser_paths:
            if intro_swish_path:
                last_teaser_idx = len(teaser_paths) - 1
for i, tp in enumerate(teaser_paths):
    teaser_sequence.append(tp)
    if i == last_teaser_idx:
        # No swish after "But first," — it leads directly into story 1
        pass
    else:
        teaser_sequence.append(intro_swish_path)
            else:
                teaser_sequence = list(teaser_paths)

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
        # Voice plays, beat continues for 0.5s after voice ends, then fades out
        # Volume lowered to 0.10 to sit further under the voice
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
        # Fade out last story segment over 1.5s before throw sting fires
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
        # [intro] [teaser+music] [stories+beat] [throw] [closing+beat+tail] [outro]
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
                print(f"Downloading {len(image_urls)} Pexels images")
                image_paths = []
                for i, url in enumerate(image_urls):
                    img_path = os.path.join(tmpdir, f"slide_{i}.jpg")
                    download_pexels_image(url, img_path)
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
