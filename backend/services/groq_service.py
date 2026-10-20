from __future__ import annotations

from metadata_generator import generate_metadata


def generate_ai_metadata(filename: str, video_context: str = "") -> dict:
    return generate_metadata(filename, video_context)
