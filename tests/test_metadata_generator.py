import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from metadata_generator import _call_groq, _generate_metadata, _transcribe_audio, generate_metadata


class MetadataRepairTests(unittest.TestCase):
    def test_groq_call_accepts_optional_frame_argument(self):
        with tempfile.TemporaryDirectory() as directory:
            frame = Path(directory) / "frame.jpg"
            frame.write_bytes(b"frame")
            response = Mock()
            response.ok = True
            response.json.return_value = {
                "choices": [{"message": {"content": "{}"}}],
            }
            with (
                patch("metadata_generator.GROQ_API_KEY", "groq-test"),
                patch("metadata_generator.requests.post", return_value=response) as post,
            ):
                _call_groq("Describe the video.", [frame])

        content = post.call_args.kwargs["json"]["messages"][1]["content"]
        self.assertEqual(content[0], {"type": "text", "text": "Describe the video."})
        self.assertEqual(content[1]["type"], "image_url")

    def test_long_transcript_is_bounded_and_keeps_its_ending(self):
        transcript = "a" * 6000 + "b" * 6000 + "c" * 6000
        response = json.dumps(
            {
                "title": "A video title",
                "description": "A video description #video #topic #shorts",
            }
        )
        with patch(
            "metadata_generator._call_metadata_model",
            return_value=response,
        ) as call_model:
            _generate_metadata("clip.mp4", "", audio_transcript=transcript)

        prompt = call_model.call_args.args[0]
        bounded_transcript = prompt.split(
            "Transcript of speech from the video's audio:\n",
            maxsplit=1,
        )[1].split("\nGround the title", maxsplit=1)[0]
        self.assertLessEqual(len(bounded_transcript), 8000)
        self.assertIn("...[transcript middle omitted]...", bounded_transcript)
        self.assertTrue(bounded_transcript.endswith("c" * 2000))

    def test_video_file_frames_are_used_for_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory) / "misleading-filename.mp4"
            frame = Path(directory) / "frame.jpg"
            video.write_bytes(b"video")
            frame.write_bytes(b"frame")
            response = json.dumps(
                {
                    "title": "A title from the scene",
                    "description": "The video shows a real scene. #video #story #shorts",
                    "tags": ["scene", "story"],
                }
            )

            with (
                patch(
                    "metadata_generator._extract_video_frames",
                    return_value=[frame],
                ),
                patch("metadata_generator._extract_video_audio", return_value=None),
                patch("metadata_generator._call_groq", return_value=response) as call_groq,
            ):
                generate_metadata(video.name, video_path=video)

        self.assertEqual(call_groq.call_args.args[1], [frame])
        self.assertIn("recognizable characters", call_groq.call_args.args[0])

    def test_audio_transcript_grounds_metadata_without_sending_frames(self):
        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory) / "Video_about_workplace_safety_AI.mp4"
            audio = Path(directory) / "audio.mp3"
            video.write_bytes(b"video")
            audio.write_bytes(b"audio")
            response = json.dumps(
                {
                    "title": "Computer Vision Improves Workplace Safety",
                    "description": (
                        "See how computer vision detects unsafe workplace "
                        "conditions in real time. #ComputerVision #AI #Safety"
                    ),
                    "tags": ["computer vision", "workplace safety", "AI"],
                }
            )
            with (
                patch("metadata_generator._extract_video_audio", return_value=audio),
                patch(
                    "metadata_generator._transcribe_audio",
                    return_value="Computer vision can identify workplace hazards.",
                ),
                patch("metadata_generator._extract_video_frames") as extract_frames,
                patch("metadata_generator._call_groq", return_value=response) as call_groq,
            ):
                metadata = generate_metadata(video.name, video_path=video)

        prompt, image_paths = call_groq.call_args.args
        self.assertIsNone(image_paths)
        extract_frames.assert_not_called()
        self.assertIn("Computer vision can identify workplace hazards.", prompt)
        self.assertIn("If narration or dialogue states the subject", prompt)
        self.assertIn("computer vision", metadata["title"].lower())

    def test_empty_audio_transcription_is_explicitly_provided_to_metadata_model(self):
        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory) / "silent-speech.mp4"
            frame = Path(directory) / "frame.jpg"
            audio = Path(directory) / "audio.mp3"
            video.write_bytes(b"video")
            frame.write_bytes(b"frame")
            audio.write_bytes(b"audio")
            response = json.dumps(
                {
                    "title": "A clear visual topic",
                    "description": "A description grounded in the video. #video #topic #shorts",
                    "tags": ["video", "topic"],
                }
            )

            with (
                patch("metadata_generator._extract_video_frames", return_value=[frame]),
                patch("metadata_generator._extract_video_audio", return_value=audio),
                patch("metadata_generator._transcribe_audio", return_value=""),
                patch("metadata_generator._call_groq", return_value=response) as call_groq,
            ):
                generate_metadata(video.name, video_path=video)

        self.assertIn("[No intelligible speech detected.]", call_groq.call_args.args[0])

    def test_transcription_uses_groq_whisper_endpoint_and_returns_speech(self):
        with tempfile.TemporaryDirectory() as directory:
            audio = Path(directory) / "audio.mp3"
            audio.write_bytes(b"audio")
            response = Mock()
            response.ok = True
            response.json.return_value = {"text": "This video explains computer vision."}
            with (
                patch("metadata_generator.GROQ_API_KEY", "groq-test"),
                patch("metadata_generator.requests.post", return_value=response) as post,
            ):
                transcript = _transcribe_audio(audio)

        self.assertEqual(transcript, "This video explains computer vision.")
        self.assertEqual(post.call_args.args[0], "https://api.groq.com/openai/v1/audio/transcriptions")
        self.assertEqual(
            post.call_args.kwargs["data"]["model"],
            "whisper-large-v3-turbo",
        )
        self.assertEqual(
            post.call_args.kwargs["headers"]["Authorization"],
            "Bearer groq-test",
        )
        self.assertEqual(post.call_args.kwargs["timeout"], 120)
        response.raise_for_status.assert_called_once_with()

    def test_missing_tags_use_local_fallback_without_a_repair_request(self):
        incomplete = json.dumps(
            {
                "title": "A video title",
                "description": "A video description",
            }
        )
        with patch(
            "metadata_generator._call_groq",
            return_value=incomplete,
        ) as call_groq:
            metadata = generate_metadata("clip.mp4", "sample context")

        self.assertGreaterEqual(len(metadata["tags"]), 8)
        self.assertEqual(call_groq.call_count, 1)

    def test_missing_required_keys_after_repair_still_raise(self):
        incomplete = json.dumps(
            {
                "title": "A video title",
            }
        )

        with patch("metadata_generator._call_groq", return_value=incomplete):
            with self.assertRaisesRegex(ValueError, "missing required fields"):
                generate_metadata("clip.mp4")

    def test_missing_tags_after_repair_are_derived_from_video_metadata(self):
        incomplete = json.dumps(
            {
                "title": "A creator shares an unexpected camera moment",
                "description": (
                    "Watch an unexpected camera moment with a surprising reaction. "
                    "#viralshorts #trendingnow #caughtoncamera"
                ),
            }
        )
        with patch("metadata_generator._call_groq", return_value=incomplete):
            metadata = generate_metadata(
                "unexpected_camera_moment.mp4",
                "creator reaction clip",
            )

        self.assertGreaterEqual(len(metadata["tags"]), 8)
        self.assertIn("viralshorts", metadata["tags"])
        self.assertTrue(all(isinstance(tag, str) and tag for tag in metadata["tags"]))

    def test_groq_rate_limit_uses_configured_compatible_fallback(self):
        import requests

        response = Mock(status_code=429, headers={"Retry-After": "120"})
        groq_error = requests.HTTPError("rate limited", response=response)
        expected = json.dumps(
            {
                "title": "Fallback title",
                "description": "Fallback description #topic #video #shorts",
            }
        )
        with (
            patch("metadata_generator.GROQ_API_KEY", "groq-test"),
            patch("metadata_generator.FALLBACK_API_KEY", "fallback-test"),
            patch("metadata_generator._call_groq", side_effect=groq_error),
            patch(
                "metadata_generator._fallback_chat_completion",
                return_value=expected,
            ) as fallback,
        ):
            metadata = generate_metadata("actual-video.mp4")

        fallback.assert_called_once()
        self.assertEqual(metadata["title"], "Fallback title")
        self.assertTrue(metadata["tags"])

    def test_missing_groq_uses_configured_fallback_directly(self):
        expected = json.dumps(
            {
                "title": "Fallback title",
                "description": "Fallback description #topic #video #shorts",
            }
        )
        with (
            patch("metadata_generator.GROQ_API_KEY", ""),
            patch("metadata_generator.FALLBACK_API_KEY", "fallback-test"),
            patch(
                "metadata_generator._fallback_chat_completion",
                return_value=expected,
            ) as fallback,
        ):
            metadata = generate_metadata("actual-video.mp4")

        fallback.assert_called_once()
        self.assertEqual(metadata["title"], "Fallback title")

    def test_groq_transcription_rate_limit_uses_configured_fallback(self):
        import requests

        with tempfile.TemporaryDirectory() as directory:
            audio = Path(directory) / "audio.mp3"
            audio.write_bytes(b"audio")
            response = Mock(status_code=429, headers={"Retry-After": "300"})
            groq_error = requests.HTTPError("rate limited", response=response)
            with (
                patch("metadata_generator.GROQ_API_KEY", "groq-test"),
                patch("metadata_generator.FALLBACK_API_KEY", "fallback-test"),
                patch(
                    "metadata_generator._transcribe_audio_groq",
                    side_effect=groq_error,
                ),
                patch(
                    "metadata_generator._transcribe_audio_fallback",
                    return_value="Spoken video topic.",
                ) as fallback,
            ):
                transcript = _transcribe_audio(audio)

        self.assertEqual(transcript, "Spoken video topic.")
        fallback.assert_called_once_with(audio)


if __name__ == "__main__":
    unittest.main()
