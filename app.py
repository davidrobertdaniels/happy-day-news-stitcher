import os
import uuid
import requests
import subprocess
import tempfile
from flask import Flask, request, jsonify, send_file

app = Flask(__name__)

SWISH_URL = os.environ.get("SWISH_URL", "")
INTRO_URL = os.environ.get("INTRO_URL", "")
OUTRO_URL = os.environ.get("OUTRO_URL", "")


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


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


@app.route("/stitch", methods=["POST"])
def stitch():
    # Log raw request for debugging
    raw_body = request.get_data(as_text=True)
    print(f"Raw body received: {raw_body[:500]}")
    print(f"Content-Type: {request.content_type}")

    # Try to parse JSON
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
    swish_url = data.get("swish_url") or SWISH_URL

    if not all([intro_url, outro_url, swish_url]):
        return jsonify({"error": "Missing intro, outro or swish URL"}), 400

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

        story_paths = []
        for i, url in enumerate(stories):
            p = os.path.join(tmpdir, f"story_{i+1}.mp3")
            download_file(url, p)
            story_paths.append(p)

        sequence = [intro_path]
        for i, sp in enumerate(story_paths):
            sequence.append(sp)
            if i < len(story_paths) - 1:
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


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
