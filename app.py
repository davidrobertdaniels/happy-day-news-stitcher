import os
import uuid
import requests
import subprocess
import tempfile
from flask import Flask, request, jsonify, send_file

app = Flask(__name__)

# Fixed assets — update these paths/URLs once deployed
SWISH_URL = os.environ.get("SWISH_URL", "")
INTRO_URL = os.environ.get("INTRO_URL", "")
OUTRO_URL = os.environ.get("OUTRO_URL", "")


def download_file(url, dest_path):
    """Download a file from a URL or copy from local path."""
    if url.startswith("http"):
        r = requests.get(url, timeout=30)
        r.raise_for_status()
        with open(dest_path, "wb") as f:
            f.write(r.content)
    else:
        import shutil
        shutil.copy(url, dest_path)


def stitch_audio(file_paths, output_path):
    """Use FFmpeg to concatenate audio files."""
    # Write a concat list file for FFmpeg
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
    """
    Expects JSON body:
    {
        "stories": [
            "https://drive.google.com/uc?export=download&id=...",
            "https://drive.google.com/uc?export=download&id=...",
            ... (6 URLs)
        ],
        "intro_url": "optional override",
        "outro_url": "optional override",
        "swish_url": "optional override"
    }
    Returns the stitched MP3 file.
    """
    data = request.get_json()
    if not data or "stories" not in data:
        return jsonify({"error": "Missing 'stories' array in request body"}), 400

    stories = data["stories"]
    if len(stories) != 6:
        return jsonify({"error": f"Expected 6 story URLs, got {len(stories)}"}), 400

    intro_url = data.get("intro_url") or INTRO_URL
    outro_url = data.get("outro_url") or OUTRO_URL
    swish_url = data.get("swish_url") or SWISH_URL

    if not all([intro_url, outro_url, swish_url]):
        return jsonify({"error": "Missing intro, outro or swish URL. Set via env vars or request body."}), 400

    job_id = str(uuid.uuid4())[:8]
    tmpdir = tempfile.mkdtemp()

    try:
        # Download all files
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

        # Build the sequence:
        # intro → story1 → swish → story2 → swish → ... → story6 → outro
        sequence = [intro_path]
        for i, sp in enumerate(story_paths):
            sequence.append(sp)
            if i < len(story_paths) - 1:
                sequence.append(swish_path)
        sequence.append(outro_path)

        # Stitch
        output_path = os.path.join(tmpdir, f"episode_{job_id}.mp3")
        stitch_audio(sequence, output_path)

        return send_file(
            output_path,
            mimetype="audio/mpeg",
            as_attachment=True,
            download_name=f"happy_day_news_{job_id}.mp3"
        )

    except Exception as e:
        return jsonify({"error": str(e)}), 500

    finally:
        # Clean up temp files (except output which Flask still needs to send)
        for f in os.listdir(tmpdir):
            fp = os.path.join(tmpdir, f)
            if fp != output_path and os.path.exists(fp):
                try:
                    os.unlink(fp)
                except:
                    pass


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
