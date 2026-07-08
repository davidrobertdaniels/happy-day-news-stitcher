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
INTRO_URL = os.environ.get("INTRO_URL", "")
OUTRO_URL = os.environ.get("OUTRO_URL", "")
BACKGROUND_VIDEO_URL = os.environ.get("BACKGROUND_VIDEO_URL", "")
BACKGROUND_IMAGE_URL = os.environ.get("BACKGROUND_IMAGE_URL", "")

def download_file(url, dest_path):
    r = requests.get(url, timeout=60)
    r.raise_for_status()
    with open(dest_path, "wb") as f:
        f.write(r.content)

def stitch_audio(file_paths, output_path):
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
    result = subprocess.run(cmd, capture_output=True, text=True)
    os.unlink(list_path)
    if result.returncode != 0:
        raise RuntimeError(f"FFmpeg error: {result.stderr}")

def get_audio_duration(audio_path):
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        audio_path
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
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
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"FFmpeg video-bg error: {result.stderr}")

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
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"FFmpeg image-bg error: {result.stderr}")

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

    if len(stories) != 6:
        return jsonify({
            "error": f"Expected 6 story URLs, got {len(stories)}",
            "stories_received": stories
        }), 400

    intro_url = data.get("intro_url") or INTRO_URL
    outro_url = data.get("outro_url") or OUTRO_URL
    throw_url = data.get("throw_url") or THROW_URL

    swish_url = data.get("swish_url")
    if not swish_url:
        swish_pool = [u.strip() for u in SWISH_URLS.split(',') if u.strip()]
        if swish_pool:
            swish_url = random.choice(swish_pool)

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

        sequence = [intro_path]
        last_index = len(story_paths) - 1
        for i, sp in enumerate(story_paths):
            sequence.append(sp)
            if i == last_index - 1:
                sequence.append(throw_path)
            elif i < last_index - 1:
                sequence.append(swish_path)
        sequence.append(outro_path)

        stitch_audio(sequence, output_path)

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

    video_url = data.get("video_url") or BACKGROUND_VIDEO_URL
    image_url = data.get("image_url") or BACKGROUND_IMAGE_URL

    if not video_url and not image_url:
        return jsonify({"error": "No background video or image URL configured"}), 400

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
