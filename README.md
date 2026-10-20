# YouTube and Telegram Auto-Publisher

![License](https://img.shields.io/github/license/Abhinavkumar-07/youtube-instagram-auto-uploader)
![Last Commit](https://img.shields.io/github/last-commit/Abhinavkumar-07/youtube-instagram-auto-uploader)
![Stars](https://img.shields.io/github/stars/Abhinavkumar-07/youtube-instagram-auto-uploader?style=social)

Publishes video clips from a Google Drive folder to YouTube, a Telegram
channel, or both. Groq generates the metadata, and a Telegram bot can act as a
private control panel for selecting the destination.

---

Videos sit in a Google Drive folder. The pipeline picks them one at a time,
generates title/description/tags with Groq, and publishes to the selected
destination. YouTube videos are uploaded as unlisted and made public at the
selected slot. The CLI sends Telegram videos immediately; dashboard schedules
can deliver them at their configured date and time.

## Quick start

1. **Fork this repo**
2. Get free API access: [Groq](https://console.groq.com/keys) (metadata) +
  [Google Cloud](https://console.cloud.google.com/) (Drive/YouTube)
3. Configure the local `.env` file — see [Configure](#4-configure)
4. Keep the local Telegram bot or uploader running on the machine that owns
   `processed_log.json` and `publish_queue.json`
5. Use the local menu or Telegram bot to choose a destination and publish a video

Full setup walkthrough below if you want the details on each step.

## Publishing and state

YouTube's `publishAt` scheduling only works if the video is `private` at
upload time — it doesn't support "unlisted + auto-publish later." So the
pipeline is split in two:

| Script | Runs | Does |
|---|---|---|
| `upload_unlisted.py` | When requested | Picks the next video and publishes to YouTube, Telegram, or both |
| `publish_scheduled.py` | Every 10-15 min (checks the clock) | At the selected slot: flips queued YouTube videos to **public** |

`publish_queue.json` records each video's destination-specific status, while
`processed_log.json` prevents Drive videos from being selected again. Keep
these files on the same persistent machine as the local bot and scheduler.

### Dashboard scheduling

The web dashboard uses the FastAPI service and the same persistent queue. Start
the API from the repository root and keep it running; its background worker
checks due dashboard schedules every 15 seconds, uploads videos to YouTube,
Instagram, or Facebook, sends videos to Telegram, and records per-platform
success or failure in `publish_queue.json`.
The frontend refreshes Drive videos and schedule statuses every 30 seconds.

```powershell
python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

In a second terminal:

```powershell
cd frontend
npm run dev -- --host 127.0.0.1 --port 8443
```

Open `http://127.0.0.1:8443`. Only videos still available in Drive can be
scheduled; a video leaves Available as soon as it has an active schedule, and
completed deliveries disappear from Scheduled. Keep the API process running
for scheduled deliveries to execute. Failed deliveries remain visible with
their error message and can be retried from the dashboard.
`SCHEDULER_POLL_SECONDS` can optionally change the polling interval (minimum
5 seconds).

In the dashboard's **Automation** section, **Drive auto-publishing** can
schedule new Drive videos automatically at a custom interval from 1 to 1,440
minutes for the selected YouTube, Instagram, and/or Facebook destinations.
Saving enabled settings immediately queues available Drive videos in order,
up to the configured per-platform daily limit; later Drive uploads are queued
automatically. It only considers videos that are available (not already
scheduled or published). Manual schedules are left alone. Set a maximum number
of videos per platform per calendar day (defaults to 3, maximum 50); when that
day's quota is reached, new automatic schedules are placed on the next day with
room. The limit counts existing posts for that platform on the selected
calendar day, including manual schedules. You can change the date and time of
any pending schedule with its **Edit** button.
Keep the uploader dashboard API running. Drive scanning defaults to every
60 seconds and can be adjusted with
`DRIVE_AUTOMATION_POLL_SECONDS` (minimum 15 seconds). Sign in to Instagram or
Facebook in the Automation panel before enabling those destinations. Each
social platform reuses one persistent local Chrome browser/profile for its
scheduled uploads rather than opening a new browser for every video.

For each scheduled video, metadata is generated once and saved to its queue
item before delivery. Other platforms and retries reuse the saved title,
description, and tags. If speech is present, it is transcribed and sent as
text; sampled frames are sent to the vision model only when no intelligible
transcript is available. Frame analysis requires `ffmpeg` and `ffprobe` on
`PATH`. Audio transcription uses Groq's `whisper-large-v3-turbo` by default and
can be changed with `GROQ_TRANSCRIPTION_MODEL`. Metadata tags are optional from
the model; the app derives local tags from the generated title and description
when absent.

Groq rate-limit responses honor `Retry-After` and rate-limit reset headers. If
Groq remains unavailable, configure an OpenAI-compatible provider with
`METADATA_FALLBACK_API_KEY`, `METADATA_FALLBACK_BASE_URL`, and
`METADATA_FALLBACK_MODEL`. The fallback API key is optional and is never
required to use Groq.

Instagram and Facebook can also be selected as publishing destinations in
Automation or manual schedules. In **Automation**, use **Open login** for each
platform, sign in yourself in the Chrome window (including any security
challenges), then choose **Save login**. The app never asks for or stores your
password. Selenium uses separate persistent Chrome profiles under
`browser_profiles/`; the API must run on the same computer as Chrome. The
browser stays open and is reused for scheduled uploads while the API is
running. Stopping the API closes Chrome, but keeps its profile for later.
Facebook posting requires a Page you manage and its URL in Automation settings
(or `FACEBOOK_PAGE_URL` in `.env`). Instagram/Facebook uploads use video files
downloaded from the scheduled Google Drive video ID; they do not use the video
generator's output folder. Keep the dashboard API running for the selected
publish times. The separate `social_upload_manager.py` command is not needed
for dashboard scheduling.

### Automated video generation

The **Automation** dashboard section accepts one topic per line, stores the
prompt queue locally, and lets you configure destinations, timezone, and daily
publish times. For each topic, the configured idea model creates a brief and a
different review model checks it. The existing video generator then renders
the reviewed brief, uploads a timestamped MP4 to the configured Drive folder,
and reserves the next available slot in the same publishing queue. Failed jobs
stay visible in the dashboard and can be retried.

Run the uploader dashboard API on port 8000 as above. In a separate terminal,
start the generator backend on port 8001 and its worker:

```powershell
cd video-generator-2.0\backend
uvicorn app.main:app --host 127.0.0.1 --port 8001
```

```powershell
cd video-generator-2.0\backend
python -m app.workers.manager
```

Configure `GEMINI_API_KEY` and `GLM_API_KEY` in
`video-generator-2.0\backend\.env` (or select two different configured
providers with `AUTOMATION_IDEA_PROVIDER` and
`AUTOMATION_REVIEW_PROVIDER`). The root `.env` must also have a writable
`DRIVE_FOLDER_ID`. The automation worker runs inside the uploader dashboard
API, so keep both backends and the generator worker running.

## Default daily slots (edit in `.env`)

- **Slot A: 5:30 PM PKT**
- **Slot B: 9:30 PM PKT**

The CLI uploader uses these slots for YouTube; it sends Telegram channel videos
immediately. Dashboard and automation schedules use their selected platform
times.

## One-time setup

### 1. Google Cloud project (Drive + YouTube)

1. https://console.cloud.google.com/ → new project.
2. Enable **Google Drive API** and **YouTube Data API v3**.
3. OAuth consent screen: External, add yourself as a test user.
4. Create OAuth Client ID (Desktop app), download JSON as `client_secret.json` in this folder.

### 2. Python environment

```bash
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

### 3. Authenticate Google (one-time, opens a browser)

```bash
python oauth_setup.py
```

### 4. Configure

Copy `.env.example` to `.env` and fill in:
- `DRIVE_FOLDER_ID`
- `GROQ_API_KEY`
- `GEOAPIFY_API_KEY` (for geocoding lead addresses)
- `SLOT_A_TIME` / `SLOT_B_TIME` (defaults already set)
- `TELEGRAM_BOT_TOKEN` (from @BotFather)
- `TELEGRAM_CHAT_ID` (the channel ID; add the bot as a channel administrator)
- `TELEGRAM_ALLOWED_USER_IDS` (comma-separated numeric IDs allowed to use the bot)

### 5. Run

Publish the next video to YouTube at slot A:
```bash
python upload_unlisted.py --slot A
```

Publish to Telegram only, or to both destinations:
```bash
python upload_unlisted.py --destination telegram
python upload_unlisted.py --destination both --slot B
```

The Telegram bot offers buttons to select YouTube, Telegram, or both:
```bash
python telegram_bot.py
```

Keep the bot process running to accept control messages. Schedule the following
local command every 10–15 minutes to make queued YouTube videos public:
```bash
python publish_scheduled.py
```

### Lead finder and combined menu

Find nearby business leads by selecting a country, city, and Geoapify category:

```bash
python lead_generator.py --country Pakistan --city Lahore --category commercial --limit 20
```

Results are saved to `leads.json`. Or use the combined menu:

```bash
python run.py
```

The menu supports lead search, destination selection, YouTube publishing, and
starting the Telegram control bot.

## Local runner and GitHub Actions

The Telegram bot and local commands share JSON state on the local machine.
GitHub Actions runs on a separate filesystem, so it must not publish at the
same time: the workflow is disabled unless the repository variable
`ENABLE_GITHUB_PUBLISHING` is explicitly set to `true`. Do not enable it while
using local bot mode; doing so creates a separate queue and can publish the
same Drive video twice.

### Expiry date to track

- **Google refresh token expires in 7 days** unless the OAuth app is moved out of
  "Testing" mode. Fix: Google Cloud Console → your project → **Google Auth Platform
  → Audience** → **Publish app**. Do this once, early — it removes the 7-day limit.

## Notes

- `TELEGRAM_ALLOWED_USER_IDS` is an access control list; leave it configured
  with only trusted numeric Telegram user IDs.
- A failed Telegram delivery is retained as pending in `publish_queue.json` and
  retried the next time an upload command runs.
- Never put API keys or bot tokens in `.env.example`, source control, or logs.
