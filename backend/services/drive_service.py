from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

from auth import get_credentials

load_dotenv()


def _drive_client():
    credentials = get_credentials()
    return build("drive", "v3", credentials=credentials)


def get_drive_video(file_id: str) -> dict[str, Any] | None:
    drive = _drive_client()
    response = (
        drive.files()
        .get(
            fileId=file_id,
            fields="id, name, mimeType, size, thumbnailLink, createdTime, videoMediaMetadata(durationMillis)",
        )
        .execute()
    )
    if response.get("mimeType", "").startswith("video/"):
        metadata = response.get("videoMediaMetadata") or {}
        return {
            "id": response.get("id", ""),
            "drive_file_id": response.get("id", ""),
            "filename": response.get("name", ""),
            "thumbnail": response.get("thumbnailLink"),
            "size": int(response.get("size") or 0),
            "duration": metadata.get("durationMillis"),
            "created_at": response.get("createdTime"),
            "status": "available",
        }
    return None


def list_drive_videos(folder_id: str | None = None) -> list[dict[str, Any]]:
    """List available videos from the configured Google Drive folder."""
    drive = _drive_client()
    folder_id = folder_id or os.getenv("DRIVE_FOLDER_ID", "")
    if not folder_id:
        raise ValueError("DRIVE_FOLDER_ID is not configured")

    query = f"'{folder_id}' in parents and trashed = false and mimeType contains 'video/'"
    videos: list[dict[str, Any]] = []
    page_token = None
    while True:
        response = (
            drive.files()
            .list(
                q=query,
                fields="nextPageToken,files(id, name, mimeType, size, thumbnailLink, createdTime, videoMediaMetadata(durationMillis))",
                orderBy="createdTime desc",
                pageSize=1000,
                pageToken=page_token,
            )
            .execute()
        )

        for file in response.get("files", []):
            metadata = file.get("videoMediaMetadata") or {}
            videos.append(
                {
                    "id": file.get("id", ""),
                    "drive_file_id": file.get("id", ""),
                    "filename": file.get("name", ""),
                    "thumbnail": file.get("thumbnailLink"),
                    "size": int(file.get("size") or 0),
                    "duration": metadata.get("durationMillis"),
                    "created_at": file.get("createdTime"),
                    "status": "available",
                }
            )
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    return videos


def upload_video_to_drive(video_path: str | Path, filename: str) -> dict[str, str]:
    """Upload a generated video into the configured Drive folder."""
    folder_id = os.getenv("DRIVE_FOLDER_ID", "").strip()
    if not folder_id:
        raise ValueError("DRIVE_FOLDER_ID is not configured")
    path = Path(video_path)
    if not path.is_file():
        raise FileNotFoundError(f"Generated video does not exist: {path}")

    drive = _drive_client()
    response = (
        drive.files()
        .create(
            body={
                "name": filename,
                "parents": [folder_id],
                "mimeType": "video/mp4",
            },
            media_body=MediaFileUpload(
                str(path),
                mimetype="video/mp4",
                resumable=True,
            ),
            fields="id,name,mimeType",
        )
        .execute()
    )
    file_id = response.get("id")
    if not file_id:
        raise RuntimeError("Google Drive did not return an uploaded file ID")
    return {"id": file_id, "name": response.get("name", filename)}


def find_drive_video_by_name(filename: str) -> dict[str, str] | None:
    """Find an already-uploaded automation output after a worker restart."""
    folder_id = os.getenv("DRIVE_FOLDER_ID", "").strip()
    if not folder_id:
        raise ValueError("DRIVE_FOLDER_ID is not configured")
    escaped_name = filename.replace("\\", "\\\\").replace("'", "\\'")
    drive = _drive_client()
    response = (
        drive.files()
        .list(
            q=(
                f"'{folder_id}' in parents and name = '{escaped_name}' "
                "and trashed = false"
            ),
            fields="files(id,name,mimeType)",
            pageSize=10,
        )
        .execute()
    )
    for item in response.get("files", []):
        if item.get("mimeType", "").startswith("video/") and item.get("id"):
            return {"id": item["id"], "name": item.get("name", filename)}
    return None
