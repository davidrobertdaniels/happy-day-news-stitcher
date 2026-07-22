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
BACKGROUND_VIDEO_URL = os.environ.get("BACKGROUND_VIDEO_URL", "")
BACKGROUND_IMAGE_URL = os.environ.get("BACKGROUND_IMAGE_URL", "")

FFMPEG_TIMEOUT_SECONDS = 240

def download_file(url, dest_path):
    r = requests.get(url, timeout=60)
    r.raise_for_status()
    with open(dest_path, "wb") as f:
        f.write(r.content)

def run_ffmpeg(cmd, error_label):
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=FFMPEG_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"{error_label}: timed out after {FFMPEG_TIMEOUT_SECONDS}s (likely a bad/unreachable background source, not worth waiting out)")
    if result.returncode != 0:
        raise RuntimeError(f"{error_label}: {result.stderr}")
    return result

def stitch_audio(file_paths, output_path):
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

def mix_beat_under_audio(voice_path, beat_path, output_path):
    cmd = [
        "ffmpeg", "-y",
        "-i", voice_path,
        "-stream_loop", "-1",
        "-i", beat_path,
        "-filter_complex",
        "[1:a]volume=0.15[beat];[0:a][beat]amix=inputs=2:duration=first:dropout_transition=2[out]",
        "-map", "[out]",
        "-acodec", "libmp3lame",
        "-q:a", "2",
        output_path
    ]
    run_ffmpeg(cmd, "FFmpeg beat-mix error")

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
    """
    Builds a crossfade slideshow from multiple images, timed to fill the
    full duration of audio_path, with a short crossfade blend between
    consecutive images.
    """
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

    print(f"Parsed data keys: {list(data.keys())}")

    if "stories" not in data:
        return jsonify({
            "error": "Missing 'stories' key in JSON",
            "keys_received": list(data.keys())
        }), 400

    stories = data["stories"]
    if not isinstance(stories, list):
        return jsonify({
            "error": f"'stories' must be an array, got {type(stories).__name__}",
            "value": str(stories)[:200]
        }), 400

    if len(stories) < 1:
        return jsonify({
            "error": "Expected at least 1 story URL, got 0",
            "stories_received": stories
        }), 400

    intro_url = data.get("intro_url") or INTRO_URL
    outro_url = data.get("outro_url") or OUTRO_URL
    throw_url = data.get("throw_url")
    beat_url = data.get("beat_url") or BEAT_URL

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

    if not all([intro_url, outro_url, swish_url, throw_url]):
        return jsonify({"error": "Missing intro, outro, swish, or throw URL"}), 400

    job_id = str(uuid.uuid4())[:8]
    tmpdir = tempfile.mkdtemp()
    output_path = os.path.join(tmpdir, f"episode_{job_id}.mp3")

    try:
        intro_path = os.path.join(tmpdir, "intro.mp3")
        outro_path = os.path.join(tmpdir, "outro.mp3")
        swish_path = os.path.join(tmpdir, "swish.mp3")

        download_file(intro_url, intro_path)
        download_file(outro_url, outro_path)
        download_file(swish_url, swish_path)

        throw_path = os.path.join(tmpdir, "throw.mp3")
        download_file(throw_url, throw_path)

        story_paths = []
        for i, url in enumerate(stories):
            p = os.path.join(tmpdir, f"story_{i+1}.mp3")
            download_file(url, p)
            story_paths.append(p)

        last_index = len(story_paths) - 1
        closing_path = story_paths[last_index] if last_index >= 0 else None
        real_story_paths = story_paths[:last_index] if last_index >= 0 else []

        real_sequence = []
        real_last_index = len(real_story_paths) - 1
        for i, sp in enumerate(real_story_paths):
            real_sequence.append(sp)
            if real_last_index > 0 and i < real_last_index:
                real_sequence.append(swish_path)

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

            middle_sequence = [intro_path, final_stories_block]
        else:
            middle_sequence = [intro_path]

        if closing_path:
            middle_sequence.append(throw_path)
            middle_sequence.append(closing_path)

        middle_sequence.append(outro_path)

        stitch_audio(middle_sequence, output_path)

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

    # If the caller explicitly sent image_urls, that always wins — it should
    # never be silently overridden by a leftover BACKGROUND_VIDEO_URL env var.
    # video_url is only considered at all when image_urls wasn't provided.
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
        download_file(audio_url, audio_path)

        video_succeeded = False

        if video_url:
            try:
                video_path = os.path.join(tmpdir, "background.mp4")
                download_file(video_url, video_path)
                build_video_from_video_bg(video_path, audio_path, output_path)
                video_succeeded = True
            except Exception as e:
                print(f"Video background failed, falling back to image: {e}")
                used_fallback = True

        if not video_succeeded:
            if has_explicit_image_urls:
                image_paths = []
                for i, url in enumerate(image_urls):
                    img_path = os.path.join(tmpdir, f"slide_{i}.jpg")
                    download_file(url, img_path)
                    image_paths.append(img_path)
                build_video_from_multi_image_bg(image_paths, audio_path, output_path)
            elif image_url:
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
        return jsonify({"error": str(e), "used_fallback": used_fallback if 'used_fallback' in locals() else False}), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
