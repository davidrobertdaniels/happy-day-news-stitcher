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
    normalized_paths = []
    tmpdir = os.path.dirname(output_path)
    for idx, p in enumerate(file_paths):
        norm_path = os.path.join(tmpdir, f"norm_{idx}.mp3")
        norm_cmd = [
            "ffmpeg", "-y",
            "-i", p,
            "-ar", "44100",
            "-ac", "2",
            "-acodec", "libmp3lame",
            "-q:a", "2",
            norm_path
        ]
        norm_result = subprocess.run(norm_cmd, capture_output=True, text=True)
        if norm_result.returncode != 0:
            raise RuntimeError(f"FFmpeg
