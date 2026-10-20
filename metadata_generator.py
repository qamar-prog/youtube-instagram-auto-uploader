"""
Generates a YouTube title, description, and tags for a video using the Groq API
(free tier, OpenAI-compatible, simple bearer auth).

Swap providers here if you want a different one -- nothing else in the
pipeline needs to change as long as generate_metadata() returns the same shape.
"""

import json
import logging
import math
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import requests
from dotenv import load_dotenv

from utils import _rate_limit_delay, retry_on_transient

load_dotenv()

logger = logging.getLogger(__name__)

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "").strip()
GROQ_MODEL = os.environ.get("GROQ_MODEL", "qwen/qwen3.6-27b").strip()
GROQ_VISION_MODEL = os.environ.get("GROQ_VISION_MODEL", "qwen/qwen3.8-27b").strip()
GROQ_TRANSCRIPTION_MODEL = os.environ.get(
    "GROQ_TRANSCRIPTION_MODEL",
    "whisper-large-v3-turbo",
).strip()
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_TRANSCRIPTION_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
FALLBACK_API_KEY = os.environ.get("METADATA_FALLBACK_API_KEY", "").strip()
FALLBACK_BASE_URL = os.environ.get(
    "METADATA_FALLBACK_BASE_URL",
    "https://api.openai.com/v1",
).strip().rstrip("/")
FALLBACK_MODEL = os.environ.get(
    "METADATA_FALLBACK_MODEL",
    "gpt-4o-mini",
).strip()
FALLBACK_TRANSCRIPTION_MODEL = os.environ.get(
    "METADATA_FALLBACK_TRANSCRIPTION_MODEL",
    "whisper-1",
).strip()
MAX_TRANSCRIPTION_AUDIO_BYTES = 25 * 1024 * 1024
MAX_TRANSCRIPT_CHARS = 8000
METADATA_ANALYSIS_VERSION = 3

CONTENT_DESCRIPTION = os.environ.get("CONTENT_DESCRIPTION", "short-form video content")

SYSTEM_PROMPT = f"""Write accurate YouTube metadata for {CONTENT_DESCRIPTION}. Return only JSON:

{{"title": "...", "description": "...", "tags": ["...", "..."]}}

Use speech transcript as primary evidence and frames only as supporting context. Never infer the topic from a filename or recognizable character when speech says otherwise. Do not invent facts. Keep title under 70 characters; write 2-4 description sentences ending in 3-5 relevant hashtags. Tags are optional; if supplied, use 8-15 concise tags without #.
"""

_REQUIRED_KEYS = ("title", "description")
_TAG_STOPWORDS = {
    "about",
    "after",
    "before",
    "from",
    "have",
    "into",
    "that",
    "this",
    "their",
    "there",
    "they",
    "what",
    "when",
    "where",
    "which",
    "with",
    "will",
    "your",
}


def _extract_video_frames(video_path: str | Path, output_dir: str | Path) -> list[Path]:
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        raise RuntimeError(
            "ffmpeg and ffprobe are required to analyze video content; "
            "install FFmpeg and ensure both commands are on PATH"
        )

    probe = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(video_path),
        ],
        capture_output=True,
        check=True,
        text=True,
        timeout=30,
    )
    try:
        duration = float(probe.stdout.strip())
    except ValueError as exc:
        raise ValueError(f"Unable to read video duration: {video_path}") from exc
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError(f"Video has no positive duration: {video_path}")

    frames: list[Path] = []
    for index, fraction in enumerate((0.25, 0.75)):
        frame_path = Path(output_dir) / f"frame-{index}.jpg"
        subprocess.run(
            [
                ffmpeg,
                "-v",
                "error",
                "-ss",
                f"{duration * fraction:.3f}",
                "-i",
                str(video_path),
                "-frames:v",
                "1",
                "-vf",
                "scale=768:768:force_original_aspect_ratio=decrease",
                "-q:v",
                "4",
                "-y",
                str(frame_path),
            ],
            capture_output=True,
            check=True,
            timeout=30,
        )
        if not frame_path.is_file() or frame_path.stat().st_size == 0:
            raise RuntimeError(f"FFmpeg did not extract frame {index} from {video_path}")
        frames.append(frame_path)
    return frames


def _extract_video_audio(video_path: str | Path, output_dir: str | Path) -> Path | None:
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        raise RuntimeError(
            "ffmpeg and ffprobe are required to analyze video audio; "
            "install FFmpeg and ensure both commands are on PATH"
        )

    probe = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=index",
            "-of",
            "csv=p=0",
            str(video_path),
        ],
        capture_output=True,
        check=True,
        text=True,
        timeout=30,
    )
    if not probe.stdout.strip():
        logger.info("Video has no audio stream; generating metadata from its visual content.")
        return None

    audio_path = Path(output_dir) / "transcription-audio.mp3"
    subprocess.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-i",
            str(video_path),
            "-map",
            "0:a:0",
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-b:a",
            "48k",
            "-f",
            "mp3",
            "-y",
            str(audio_path),
        ],
        capture_output=True,
        check=True,
        timeout=180,
    )
    if not audio_path.is_file() or audio_path.stat().st_size == 0:
        raise RuntimeError(f"FFmpeg did not extract audio from {video_path}")
    if audio_path.stat().st_size > MAX_TRANSCRIPTION_AUDIO_BYTES:
        raise ValueError(
            "Compressed audio exceeds Groq's 25 MB transcription upload limit: "
            f"{video_path}"
        )
    return audio_path


@retry_on_transient(max_attempts=4, base_delay=2.0, max_retry_delay=60)
def _transcribe_audio_groq(audio_path: str | Path) -> str:
    """Transcribe speech from the video's audio track with Groq Whisper."""
    if not GROQ_API_KEY:
        raise RuntimeError("GROQ_API_KEY is not configured for audio transcription")
    with Path(audio_path).open("rb") as audio_file:
        response = requests.post(
            GROQ_TRANSCRIPTION_URL,
            files={"file": (Path(audio_path).name, audio_file, "audio/mpeg")},
            data={
                "model": GROQ_TRANSCRIPTION_MODEL,
                "response_format": "json",
                "temperature": "0",
            },
            headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
            timeout=120,
        )
    if not response.ok:
        logger.error("Groq transcription API error %s: %s", response.status_code, response.text)
    response.raise_for_status()
    result = response.json()
    transcript = result.get("text")
    if not isinstance(transcript, str):
        raise ValueError("Groq transcription response did not contain text")
    return transcript.strip()


def _is_provider_failure(exc: Exception) -> bool:
    if isinstance(exc, requests.HTTPError):
        response = exc.response
        return response is not None and (
            response.status_code == 429 or response.status_code >= 500
        )
    return isinstance(exc, (requests.ConnectionError, requests.Timeout))


def _transcribe_audio(audio_path: str | Path) -> str:
    if not GROQ_API_KEY:
        if FALLBACK_API_KEY:
            return _transcribe_audio_fallback(audio_path)
        raise RuntimeError(
            "Neither GROQ_API_KEY nor METADATA_FALLBACK_API_KEY is configured."
        )
    try:
        return _transcribe_audio_groq(audio_path)
    except (requests.HTTPError, requests.ConnectionError, requests.Timeout) as exc:
        if not _is_provider_failure(exc):
            raise
        if not FALLBACK_API_KEY:
            raise RuntimeError(
                "Groq audio transcription is unavailable and no fallback provider "
                "is configured. Configure METADATA_FALLBACK_API_KEY to enable "
                "automatic fallback."
            ) from exc
        logger.warning("Groq transcription unavailable; using the configured fallback provider.")
        return _transcribe_audio_fallback(audio_path)


@retry_on_transient(max_attempts=4, base_delay=2.0, max_retry_delay=60)
def _call_groq(
    user_prompt: str,
    image_paths: list[Path] | None = None,
) -> str:
    """Fire a single Groq request and return the raw content string."""
    if not GROQ_API_KEY:
        raise RuntimeError("GROQ_API_KEY is not configured")
    if image_paths:
        import base64

        content: str | list[dict] = [
            {"type": "text", "text": user_prompt},
        ]
        content.extend(
            {
                "type": "image_url",
                "image_url": {
                    "url": (
                        "data:image/jpeg;base64,"
                        + base64.b64encode(path.read_bytes()).decode("ascii")
                    )
                },
            }
            for path in image_paths
        )
    else:
        content = user_prompt
    model = GROQ_VISION_MODEL if image_paths else GROQ_MODEL
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": content},
        ],
        "temperature": 0.7,
        "max_tokens": 500,
    }

    response = requests.post(
        GROQ_URL,
        json=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {GROQ_API_KEY}",
        },
        timeout=30,
    )
    if not response.ok:
        logger.error("Groq API error %s: %s", response.status_code, response.text)
    response.raise_for_status()
    return response.json()["choices"][0]["message"]["content"].strip()


@retry_on_transient(max_attempts=3, base_delay=2.0, max_retry_delay=60)
def _fallback_chat_completion(user_prompt: str) -> str:
    if not FALLBACK_API_KEY:
        raise RuntimeError(
            "Groq metadata generation failed and no fallback provider is configured. "
            "Set METADATA_FALLBACK_API_KEY and, if needed, "
            "METADATA_FALLBACK_BASE_URL and METADATA_FALLBACK_MODEL."
        )
    response = requests.post(
        f"{FALLBACK_BASE_URL}/chat/completions",
        json={
            "model": FALLBACK_MODEL,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.3,
            "max_tokens": 400,
        },
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {FALLBACK_API_KEY}",
        },
        timeout=45,
    )
    response.raise_for_status()
    return response.json()["choices"][0]["message"]["content"].strip()


def _call_metadata_model(
    user_prompt: str,
    image_paths: list[Path] | None = None,
) -> str:
    if not GROQ_API_KEY:
        if FALLBACK_API_KEY:
            logger.warning("Groq is not configured; using the configured metadata fallback provider.")
            fallback_prompt = user_prompt
            if image_paths:
                fallback_prompt += (
                    "\nThe fallback model receives text only, not the sampled frames. "
                    "Do not infer visual details that are not in the filename or transcript."
                )
            return _fallback_chat_completion(fallback_prompt)
        raise RuntimeError(
            "Neither GROQ_API_KEY nor METADATA_FALLBACK_API_KEY is configured."
        )
    try:
        return _call_groq(user_prompt, image_paths)
    except (requests.HTTPError, requests.ConnectionError, requests.Timeout) as exc:
        if not _is_provider_failure(exc):
            raise
        if not FALLBACK_API_KEY:
            retry_delay = (
                _rate_limit_delay(exc)
                if isinstance(exc, requests.HTTPError)
                and exc.response is not None
                and exc.response.status_code == 429
                else None
            )
            wait_note = (
                f" Groq reports a retry/reset delay of {retry_delay:g} seconds."
                if retry_delay is not None
                else ""
            )
            raise RuntimeError(
                "Groq metadata generation is unavailable after retries and no "
                "fallback provider is configured. Set METADATA_FALLBACK_API_KEY "
                "to enable automatic fallback." + wait_note
            ) from exc
        logger.warning("Groq metadata generation unavailable; using the configured fallback provider.")
        fallback_prompt = user_prompt
        if image_paths:
            fallback_prompt += (
                "\nThe fallback model receives text only, not the sampled frames. "
                "Do not infer visual details that are not in the filename or transcript."
            )
        return _fallback_chat_completion(fallback_prompt)


@retry_on_transient(max_attempts=3, base_delay=2.0, max_retry_delay=60)
def _transcribe_audio_fallback(audio_path: str | Path) -> str:
    if not FALLBACK_API_KEY:
        raise RuntimeError(
            "Groq audio transcription failed and no fallback provider is configured. "
            "Set METADATA_FALLBACK_API_KEY and METADATA_FALLBACK_TRANSCRIPTION_MODEL."
        )
    with Path(audio_path).open("rb") as audio_file:
        response = requests.post(
            f"{FALLBACK_BASE_URL}/audio/transcriptions",
            files={"file": (Path(audio_path).name, audio_file, "audio/mpeg")},
            data={
                "model": FALLBACK_TRANSCRIPTION_MODEL,
                "response_format": "json",
                "temperature": "0",
            },
            headers={"Authorization": f"Bearer {FALLBACK_API_KEY}"},
            timeout=120,
        )
    response.raise_for_status()
    transcript = response.json().get("text")
    if not isinstance(transcript, str):
        raise ValueError("Fallback transcription response did not contain text")
    return transcript.strip()


def _parse_metadata(raw: str) -> dict:
    """Parse and merge JSON objects returned by the model."""
    cleaned = raw.replace("```json", "").replace("```", "").strip()
    decoder = json.JSONDecoder()
    parsed_objects = []
    for index, character in enumerate(cleaned):
        if character == "{":
            try:
                data, _ = decoder.raw_decode(cleaned[index:])
                if isinstance(data, dict):
                    parsed_objects.append(data)
            except json.JSONDecodeError:
                continue
    if parsed_objects:
        merged = {}
        for data in parsed_objects:
            merged.update(data)
        return merged
    raise json.JSONDecodeError("No JSON object found in model response", cleaned, 0)


def _fallback_tags(filename: str, extra_context: str, title: str, description: str) -> list[str]:
    """Build usable tags from the supplied content when Groq omits its tags field."""
    candidates = re.findall(r"#([\w-]+)", description)
    candidates.extend(re.findall(r"[\w'-]+", title))
    candidates.extend(re.findall(r"[\w'-]+", extra_context))
    candidates.extend(re.findall(r"[\w'-]+", os.path.splitext(os.path.basename(filename))[0]))

    tags: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        tag = re.sub(r"[_-]+", " ", candidate).strip().lower()
        if len(tag) < 2 or tag in _TAG_STOPWORDS or tag in seen:
            continue
        seen.add(tag)
        tags.append(tag)
        if len(tags) == 15:
            break

    if len(tags) < 8:
        for tag in ("video", "short video", "video clip", "content creator", "online video"):
            if tag not in seen:
                seen.add(tag)
                tags.append(tag)
            if len(tags) == 8:
                break
    return tags


def _generate_metadata(
    filename: str,
    extra_context: str,
    image_paths: list[Path] | None = None,
    audio_transcript: str | None = None,
) -> dict:
    user_prompt = f"Source filename (weak hint only): {filename}"
    if extra_context:
        user_prompt += f"\nOptional context (not verified): {extra_context}"
    if image_paths:
        user_prompt += (
            "\nAnalyze the supplied frames sampled across the actual video. "
            "Identify the real subject and action rather than focusing on "
            "recognizable characters or background details alone."
        )
    if audio_transcript is not None:
        if len(audio_transcript) > MAX_TRANSCRIPT_CHARS:
            omitted_marker = "\n...[transcript middle omitted]...\n"
            leading_chars = MAX_TRANSCRIPT_CHARS - len(omitted_marker) - 2000
            audio_transcript = (
                audio_transcript[:leading_chars]
                + omitted_marker
                + audio_transcript[-2000:]
            )
        user_prompt += (
            "\nTranscript of speech from the video's audio:\n"
            f"{audio_transcript or '[No intelligible speech detected.]'}"
        )
    if image_paths or audio_transcript is not None:
        user_prompt += (
            "\nGround the title, description, and tags in the actual video. "
            "If narration or dialogue states the subject, make that the main "
            "topic and use the frames as supporting visual context. Do not "
            "mistake a visible character, meme, or filename for the subject "
            "when the speech describes another topic."
        )

    raw = _call_metadata_model(user_prompt, image_paths)
    data = _parse_metadata(raw)

    missing = [
        key
        for key in _REQUIRED_KEYS
        if not isinstance(data.get(key), str) or not data[key].strip()
    ]
    if missing:
        logger.warning(
            "Groq response omitted required metadata keys %s; requesting one corrected response.",
            missing,
        )
        repair_prompt = (
            f"{user_prompt}\n\n"
            f"The prior JSON response omitted these required keys: {', '.join(missing)}.\n"
            "Return a corrected JSON object containing a non-empty title and description. "
            "Tags are optional; if omitted, the application adds safe local defaults.\n"
            f"Prior response: {raw}"
        )
        raw = _call_metadata_model(repair_prompt, image_paths)
        data = _parse_metadata(raw)
        missing_required = [
            key
            for key in _REQUIRED_KEYS
            if not isinstance(data.get(key), str) or not data[key].strip()
        ]
        if missing_required:
            raise ValueError(
                f"Metadata response is still missing required fields: {missing_required}. "
                f"Raw response: {raw!r}"
            )

    tags = data.get("tags")
    if not isinstance(tags, list) or not any(
        isinstance(tag, str) and tag.strip() for tag in tags
    ):
        logger.warning(
            "Groq did not return usable tags; deriving tags from the generated metadata and source context."
        )
        tags = _fallback_tags(
            filename,
            extra_context,
            str(data["title"]),
            str(data["description"]),
        )
    else:
        tags = list(dict.fromkeys(tag.strip() for tag in tags if isinstance(tag, str) and tag.strip()))
    data["tags"] = tags
    return data


def generate_metadata(
    filename: str,
    extra_context: str = "",
    *,
    video_path: str | Path | None = None,
) -> dict:
    """Return {"title": str, "description": str, "tags": list[str]}.

    Args:
        filename: original video filename for context.
        extra_context: optional free-text context appended to the prompt
                       (e.g. episode number, guest name, show notes snippet).
        video_path: optional local video file to analyze using sampled frames.

    Raises:
        ValueError: if the Groq response is missing required keys.
        requests.HTTPError: on unrecoverable API errors (after retries).
        json.JSONDecodeError: if the model returns malformed JSON.
    """
    if video_path is None:
        return _generate_metadata(filename, extra_context)

    video = Path(video_path)
    if not video.is_file():
        raise FileNotFoundError(f"Video to analyze was not found: {video}")
    with tempfile.TemporaryDirectory(prefix="video-metadata-") as frame_directory:
        audio_path = _extract_video_audio(video, frame_directory)
        audio_transcript = (
            _transcribe_audio(audio_path)
            if audio_path is not None
            else None
        )
        if audio_transcript:
            image_paths = None
        else:
            image_paths = _extract_video_frames(video, frame_directory)
            if audio_path is not None:
                audio_transcript = ""
        return _generate_metadata(
            filename,
            extra_context,
            image_paths,
            audio_transcript,
        )


if __name__ == "__main__":
    # quick manual test
    logging.basicConfig(level=logging.INFO)
    result = generate_metadata("ep12_clip_founder_burnout.mp4")
    print(json.dumps(result, indent=2))
