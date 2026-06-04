"""Subtitle extraction helpers."""

import logging
import re
import urllib.request
from typing import Dict, List, Tuple

from .yt_dlp_utils import build_yt_dlp_opts, run_with_retries

logger = logging.getLogger(__name__)


class YouTubeSubtitleExtractor:
    """
    Extracts auto-generated subtitles from YouTube live streams.
    Uses youtube-transcript-api to fetch available subtitles.
    """

    def __init__(self, video_id: str):
        self.video_id = self.extract_video_id(video_id)
        self.last_segment_index = 0
        self.last_timestamp = 0.0

    def extract_video_id(self, url_or_id: str) -> str:
        """Extract video ID from YouTube URL or return if already an ID.

        Delegates to the shared pipeline helper so all URL formats
        (/watch, /live/, /embed/, /shorts/, youtu.be) stay in sync.
        """
        from .pipeline import extract_video_id as _extract
        return _extract(url_or_id)

    def check_subtitles_available(self) -> Tuple[bool, List[str]]:
        """
        Check if subtitles are available for the video.
        Returns: (available, list of available languages)
        """
        try:
            from youtube_transcript_api import YouTubeTranscriptApi
            from youtube_transcript_api._errors import NoTranscriptFound, TranscriptsDisabled

            available_languages = []
            api = YouTubeTranscriptApi()

            try:
                transcript_list = api.list(self.video_id)

                for transcript in transcript_list:
                    if not transcript.is_generated:
                        available_languages.append(transcript.language_code)

                for transcript in transcript_list:
                    if transcript.is_generated:
                        lang_label = f"{transcript.language_code} (auto-generated)"
                        if lang_label not in available_languages:
                            available_languages.append(lang_label)

                # If we found transcripts via list_transcripts, return them
                if available_languages:
                    return True, available_languages

            except (AttributeError, NoTranscriptFound):
                pass

            for lang in ['en', 'en-US', 'en-GB', 'ru', 'es', 'fr', 'de']:
                try:
                    api.fetch(self.video_id, languages=[lang])
                    available_languages.append(lang)
                    break  # Found at least one, that's enough
                except Exception:
                    continue

            return len(available_languages) > 0, available_languages

        except TranscriptsDisabled:
            logger.warning("Transcripts are disabled for this video.")
            return False, []
        except NoTranscriptFound:
            logger.warning("No transcripts found for this video.")
            return False, []
        except Exception as e:
            logger.debug(f"Error checking subtitles: {e}")
            import traceback
            logger.debug(traceback.format_exc())
            return False, []

    def get_new_subtitles(self, language: str = "en") -> List[Dict]:
        """
        Get new subtitle segments since last check.
        Returns list of segments with 'text', 'start', and 'duration' keys.
        """
        try:
            from youtube_transcript_api import YouTubeTranscriptApi
            from youtube_transcript_api._errors import NoTranscriptFound, TranscriptsDisabled

            api = YouTubeTranscriptApi()

            try:
                transcript = api.fetch(self.video_id, languages=[language])
            except NoTranscriptFound:
                try:
                    transcript_list = api.list(self.video_id)
                    transcript_data = transcript_list.find_generated_transcript([language])
                    transcript = transcript_data.fetch()
                except NoTranscriptFound:
                    try:
                        transcript_list = api.list(self.video_id)
                        transcript_data = transcript_list.find_transcript(['en', 'en-US', 'en-GB'])
                        transcript = transcript_data.fetch()
                        logger.info(f"Using transcript in language: {transcript_data.language_code}")
                    except Exception:
                        try:
                            transcript_list = api.list(self.video_id)
                            for transcript_item in transcript_list:
                                transcript = transcript_item.fetch()
                                logger.info(f"Using transcript in language: {transcript_item.language_code}")
                                break
                        except Exception as e2:
                            logger.error(f"Could not find any transcript: {e2}")
                            raise NoTranscriptFound(self.video_id, [language], {})

            if hasattr(transcript, "to_raw_data"):
                transcript = transcript.to_raw_data()

            # Filter segments that are new (after last_timestamp)
            # For first call (last_timestamp == 0), return all segments
            if self.last_timestamp == 0.0:
                new_segments = transcript
            else:
                new_segments = [
                    seg for seg in transcript
                    if seg['start'] >= self.last_timestamp
                ]

            # Update last timestamp if we got new segments
            if new_segments:
                self.last_timestamp = new_segments[-1]['start'] + new_segments[-1]['duration']

            return new_segments

        except TranscriptsDisabled:
            logger.error("Transcripts are disabled for this video.")
            return []
        except NoTranscriptFound:
            logger.error(f"No transcript found for language: {language}")
            return []
        except Exception as e:
            logger.error(f"Error fetching subtitles: {e}")
            import traceback
            logger.debug(traceback.format_exc())
            return []


class YtDlpSubtitleExtractor:
    """Extract subtitle URLs exposed by yt-dlp for non-YouTube remotes."""

    def __init__(
        self,
        url: str,
        *,
        yt_dlp_cookie_file: str = None,
        yt_dlp_cookies_from_browser: str = None,
        retries: int = 3,
        socket_timeout: int = 20,
    ):
        self.url = url
        self.last_timestamp = 0.0
        self._yt_dlp_cookie_file = yt_dlp_cookie_file
        self._yt_dlp_cookies_from_browser = yt_dlp_cookies_from_browser
        self._retries = retries
        self._socket_timeout = socket_timeout
        self._info = None

    def _opts(self, **overrides):
        return build_yt_dlp_opts(
            yt_dlp_cookie_file=self._yt_dlp_cookie_file,
            yt_dlp_cookies_from_browser=self._yt_dlp_cookies_from_browser,
            retries=self._retries,
            socket_timeout=self._socket_timeout,
            **overrides,
        )

    def _get_info(self):
        if self._info is not None:
            return self._info
        try:
            import yt_dlp
        except ImportError:
            logger.error("yt-dlp not installed. Install with: pip install yt-dlp")
            return None

        opts = self._opts(
            format="bestaudio/best",
            skip_download=True,
            writesubtitles=True,
            writeautomaticsub=True,
            subtitleslangs=["all"],
        )

        def fetch():
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(self.url, download=False)

        try:
            self._info = run_with_retries(
                "yt-dlp subtitle metadata",
                fetch,
                attempts=self._retries,
            )
            return self._info
        except Exception as exc:
            logger.warning("Could not probe remote subtitles: %s", exc)
            return None

    @staticmethod
    def _language_map(info):
        if not info:
            return {}
        merged = {}
        for key in ("subtitles", "automatic_captions"):
            tracks = info.get(key) or {}
            for language, entries in tracks.items():
                if entries:
                    merged.setdefault(language, []).extend(entries)
        return merged

    @staticmethod
    def _pick_track(language_map, language):
        preferred_languages = [language, "en", "en-US", "en-GB"]
        for lang in preferred_languages:
            if lang in language_map:
                entries = language_map[lang]
                break
        else:
            if not language_map:
                return None, None
            lang, entries = next(iter(language_map.items()))

        preferred_exts = ("vtt", "srt", "ttml")
        for ext in preferred_exts:
            for entry in entries:
                if (entry.get("ext") or "").lower() == ext and entry.get("url"):
                    return lang, entry
        for entry in entries:
            if entry.get("url"):
                return lang, entry
        return lang, None

    def check_subtitles_available(self) -> Tuple[bool, List[str]]:
        info = self._get_info()
        language_map = self._language_map(info)
        languages = sorted(language_map)
        if languages:
            logger.info("yt-dlp exposed subtitles in: %s", ", ".join(languages))
        else:
            logger.info("No remote subtitles exposed by yt-dlp; Whisper fallback is expected")
        return bool(languages), languages

    def get_new_subtitles(self, language: str = "en") -> List[Dict]:
        info = self._get_info()
        language_map = self._language_map(info)
        selected_language, track = self._pick_track(language_map, language)
        if not track:
            return []
        try:
            data = self._download_track(track["url"])
            segments = self._parse_subtitle_text(data)
            if self.last_timestamp == 0.0:
                new_segments = segments
            else:
                new_segments = [seg for seg in segments if seg["start"] >= self.last_timestamp]
            if new_segments:
                last = new_segments[-1]
                self.last_timestamp = last["start"] + last["duration"]
            logger.info(
                "Using %s subtitle track from yt-dlp (%d new segments)",
                selected_language,
                len(new_segments),
            )
            return new_segments
        except Exception as exc:
            logger.warning("Failed to fetch remote subtitle track: %s", exc)
            return []

    def _download_track(self, url: str) -> str:
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0",
                "Accept": "text/vtt,text/plain,*/*",
            },
        )
        with urllib.request.urlopen(request, timeout=self._socket_timeout) as response:
            raw = response.read()
        return raw.decode("utf-8", errors="replace")

    @staticmethod
    def _timestamp_to_seconds(value: str) -> float:
        match = re.match(r"(?:(\d+):)?(\d{2}):(\d{2})(?:[.,](\d{1,3}))?", value.strip())
        if not match:
            return 0.0
        hours = int(match.group(1) or 0)
        minutes = int(match.group(2) or 0)
        seconds = int(match.group(3) or 0)
        millis = (match.group(4) or "0").ljust(3, "0")[:3]
        return hours * 3600 + minutes * 60 + seconds + int(millis) / 1000.0

    @classmethod
    def _parse_subtitle_text(cls, text: str) -> List[Dict]:
        segments = []
        current_time = None
        current_lines = []

        def flush():
            nonlocal current_time, current_lines
            if current_time and current_lines:
                start, end = current_time
                seg_text = " ".join(line.strip() for line in current_lines if line.strip())
                seg_text = re.sub(r"<[^>]+>", "", seg_text).strip()
                if seg_text:
                    segments.append({
                        "text": seg_text,
                        "start": start,
                        "duration": max(0.0, end - start),
                    })
            current_time = None
            current_lines = []

        for raw_line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
            line = raw_line.strip("\ufeff").strip()
            if not line:
                flush()
                continue
            if line.upper().startswith("WEBVTT") or line.upper().startswith("NOTE"):
                continue
            if "-->" in line:
                flush()
                start_raw, end_raw = [part.strip().split()[0] for part in line.split("-->", 1)]
                current_time = (cls._timestamp_to_seconds(start_raw), cls._timestamp_to_seconds(end_raw))
                continue
            if current_time is not None and not line.isdigit():
                current_lines.append(line)
        flush()
        return segments
