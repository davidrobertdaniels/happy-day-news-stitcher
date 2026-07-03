# Happy Day News — Audio Stitcher

A lightweight Flask app that stitches your daily podcast episode together using FFmpeg.

## What it does

Takes 6 story audio clips and combines them into one finished MP3:

```
Intro → Story 1 → Swish → Story 2 → Swish → Story 3 → Swish → Story 4 → Swish → Story 5 → Swish → Story 6 → Outro
```

## Deploy to Render (free)

1. Create a GitHub account if you don't have one
2. Create a new GitHub repository called `happy-day-news-stitcher`
3. Upload these files to it (app.py, requirements.txt, render.yaml, README.md)
4. Go to render.com and sign up with your GitHub account
5. Click "New" → "Web Service" → connect your GitHub repo
6. Render will auto-detect the config from render.yaml
7. In the Environment Variables section, set:
   - `SWISH_URL` — public URL to your swish MP3
   - `INTRO_URL` — public URL to your intro MP3
   - `OUTRO_URL` — public URL to your outro MP3
8. Click Deploy

Your app URL will be something like: `https://happy-day-news-stitcher.onrender.com`

## API Usage

### Health check
```
GET https://your-app.onrender.com/health
```

### Stitch an episode
```
POST https://your-app.onrender.com/stitch
Content-Type: application/json

{
  "stories": [
    "https://drive.google.com/uc?export=download&id=STORY1_ID",
    "https://drive.google.com/uc?export=download&id=STORY2_ID",
    "https://drive.google.com/uc?export=download&id=STORY3_ID",
    "https://drive.google.com/uc?export=download&id=STORY4_ID",
    "https://drive.google.com/uc?export=download&id=STORY5_ID",
    "https://drive.google.com/uc?export=download&id=STORY6_ID"
  ]
}
```

Returns the finished MP3 file as a download.

You can also override the fixed assets per-request:
```json
{
  "stories": [...],
  "swish_url": "https://...",
  "intro_url": "https://...",
  "outro_url": "https://..."
}
```

## Wiring into n8n

After your Collect Full Articles node, add an HTTP Request node:
- Method: POST
- URL: https://your-app.onrender.com/stitch
- Body: JSON with the 6 story Google Drive URLs
- Response Format: File

The returned file is your finished episode MP3, ready to email or upload to Spotify.

## Notes

- FFmpeg is pre-installed on Render's free Python environment
- The free tier sleeps after 15 minutes of inactivity — first call after sleep takes ~30 seconds to wake up. Your daily n8n run will handle this fine since it waits for responses.
- All temp files are cleaned up after each request
- The swish sting plays between stories, not before story 1 or after story 6
