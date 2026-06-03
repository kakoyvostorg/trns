"""
Whisper Transcriber Module

Transcribes audio using OpenAI Whisper (or faster-whisper for better performance).
Uses tiny model for English, small model for other languages.
Extracts audio from YouTube video and processes it in chunks.
"""

import logging
import os
import re
import time
from typing import Optional, Tuple

logger = logging.getLogger(__name__)


def _is_local_file(path: str) -> bool:
    """Check if a string is a local file path (not a URL or bare video ID)."""
    if path.startswith("http://") or path.startswith("https://"):
        return False
    return os.path.exists(path)


class WhisperTranscriber:
    """
    Transcribes audio using OpenAI Whisper (or faster-whisper for better performance).
    Uses tiny model for English, small model for other languages.
    Extracts audio from YouTube video and processes it in chunks.
    """

    def __init__(self, model_size: str = "auto", use_faster_whisper: bool = True, shutdown_flag=None):
        """
        Initialize Whisper transcriber.

        Args:
            model_size: Whisper model size. "auto" (default) selects dynamically
                        (tiny for English, small for other languages).
                        Explicit values: "tiny", "base", "small", "medium", "large".
            use_faster_whisper: Use faster-whisper library (faster, more efficient)
            shutdown_flag: Optional callable/object to check for shutdown signal
        """
        self.model_size = model_size
        self.use_faster_whisper = use_faster_whisper
        self.language_detector = None  # Tiny model for quick language detection
        self.tiny_model = None  # Tiny model for English transcription
        self.small_model = None  # Small model for non-English transcription
        self.explicit_model = None  # User-specified model (when not "auto")
        self.shutdown_flag = shutdown_flag
        # Cache for video info to avoid redundant fetching
        self._video_info_cache = {}  # {video_id: {'info': dict, 'timestamp': float, 'is_live': bool}}
        self._cache_ttl = 300  # Cache for 5 minutes (300 seconds)
        # Cache for translators to avoid redundant instantiation
        self._translator_cache = {}  # {source_language: GoogleTranslator instance}
        self._last_detected_language = None  # Cache last detected language
        self._initialize_model()

    def _check_shutdown(self) -> bool:
        """Check if shutdown was requested"""
        if self.shutdown_flag is None:
            return False
        if callable(self.shutdown_flag):
            return self.shutdown_flag()
        return bool(self.shutdown_flag)

    @staticmethod
    def _cleanup_temp_files(base_path: str):
        """Remove any temp audio files matching the base path (all extensions)."""
        try:
            import glob
            for f in glob.glob(base_path + ".*"):
                try:
                    os.remove(f)
                except OSError:
                    pass
            # Also try the base path itself
            if os.path.exists(base_path):
                try:
                    os.remove(base_path)
                except OSError:
                    pass
        except Exception:
            pass

    def _initialize_model(self):
        """Initialize the Whisper models.

        When model_size is "auto": tiny for language detection + English, small lazy-loaded for others.
        When model_size is explicit: that model is used for everything (detection + transcription).
        """
        try:
            if self.use_faster_whisper:
                try:
                    import os

                    from faster_whisper import WhisperModel
                    cpu_threads = max(1, os.cpu_count() - 1) if os.cpu_count() else 4
                    logger.info(f"Initializing faster-whisper with {cpu_threads} CPU threads...")

                    if self.model_size != "auto":
                        # User specified an explicit model — use it for everything
                        logger.info(f"Initializing faster-whisper {self.model_size} model (user-selected)...")
                        self.explicit_model = WhisperModel(
                            self.model_size,
                            device="cpu",
                            compute_type="int8",
                            cpu_threads=cpu_threads,
                            num_workers=1
                        )
                        self.language_detector = self.explicit_model
                        self.tiny_model = self.explicit_model
                        logger.info(f"{self.model_size} model loaded successfully")
                    else:
                        # Auto mode: tiny for detection/English, small lazy-loaded for others
                        logger.info("Initializing faster-whisper tiny model (for language detection and English)...")
                        self.language_detector = WhisperModel(
                            "tiny",
                            device="cpu",
                            compute_type="int8",
                            cpu_threads=cpu_threads,
                            num_workers=1
                        )
                        self.tiny_model = self.language_detector
                        logger.info("Tiny model loaded successfully")
                        logger.info("Small model will be loaded on-demand for non-English languages")
                except ImportError:
                    logger.warning("faster-whisper not available, falling back to openai-whisper")
                    self.use_faster_whisper = False

            if not self.use_faster_whisper:
                try:
                    import whisper
                except ImportError:
                    raise ImportError(
                        "Neither faster-whisper nor openai-whisper is installed. "
                        "Install one of them:\n"
                        "  pip install faster-whisper   (recommended)\n"
                        "  pip install openai-whisper   (or: pip install .[whisper])"
                    )
                if self.model_size != "auto":
                    logger.info(f"Initializing openai-whisper {self.model_size} model (user-selected)...")
                    self.explicit_model = whisper.load_model(self.model_size)
                    self.language_detector = self.explicit_model
                    self.tiny_model = self.explicit_model
                    logger.info(f"{self.model_size} model loaded successfully")
                else:
                    logger.info("Initializing openai-whisper tiny model (for language detection and English)...")
                    self.language_detector = whisper.load_model("tiny")
                    self.tiny_model = self.language_detector
                    logger.info("Tiny model loaded successfully")
                    logger.info("Small model will be loaded on-demand for non-English languages")

        except ImportError:
            logger.error("Whisper library not installed. Install with: pip install faster-whisper (or openai-whisper)")
            raise
        except Exception as e:
            logger.error(f"Error initializing Whisper model: {e}")
            raise

    def _get_transcription_model(self, language: str):
        """
        Get the appropriate model for transcription based on language.

        If user specified an explicit model, always returns that.
        In auto mode: English uses tiny, others use small (lazy-loaded).

        Args:
            language: Detected language code

        Returns:
            Whisper model instance
        """
        # Explicit model overrides auto-selection
        if self.explicit_model is not None:
            return self.explicit_model

        # Auto mode: English uses tiny model
        if language == "en":
            return self.tiny_model

        # Auto mode: Non-English uses small model (load lazily)
        if self.small_model is None:
            logger.info("Loading small model for non-English transcription...")
            if self.use_faster_whisper:
                import os

                from faster_whisper import WhisperModel
                cpu_threads = max(1, os.cpu_count() - 1) if os.cpu_count() else 4
                self.small_model = WhisperModel(
                    "small",
                    device="cpu",
                    compute_type="int8",
                    cpu_threads=cpu_threads,
                    num_workers=1
                )
            else:
                import whisper
                self.small_model = whisper.load_model("small")
            logger.info("Small model loaded successfully")

        return self.small_model

    def _get_video_info(self, video_id: str, force_refresh: bool = False) -> Optional[dict]:
        """
        Get video info, using cache if available and not expired.

        Args:
            video_id: YouTube video ID or URL (for Twitter/X.com support)
            force_refresh: Force refresh even if cache is valid

        Returns:
            Video info dict or None if failed
        """
        import yt_dlp

        # Determine URL: local file, full URL, or YouTube video ID
        if _is_local_file(video_id):
            url = video_id
        elif video_id.startswith("http://") or video_id.startswith("https://"):
            url = video_id
        else:
            url = f"https://www.youtube.com/watch?v={video_id}"
        current_time = time.time()

        # Check cache
        if not force_refresh and video_id in self._video_info_cache:
            cached = self._video_info_cache[video_id]
            age = current_time - cached['timestamp']

            if age < self._cache_ttl:
                logger.debug(f"Using cached video info (age: {age:.1f}s)")
                return cached['info']
            else:
                logger.debug(f"Cache expired (age: {age:.1f}s), refreshing...")

        # Fetch fresh info
        logger.info(f"Fetching video info for: {video_id}")
        ydl_opts_info = {
            'format': 'bestaudio/best',
            'quiet': True,
            'no_warnings': True,
            # Add headers to avoid 403 errors
            'http_headers': {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                'Accept-Language': 'en-us,en;q=0.5',
                'Accept-Encoding': 'gzip, deflate',
                'Connection': 'keep-alive',
            },
        }

        try:
            with yt_dlp.YoutubeDL(ydl_opts_info) as ydl:
                logger.info("Extracting video info...")
                info = ydl.extract_info(url, download=False)

                if info:
                    # Cache the info
                    self._video_info_cache[video_id] = {
                        'info': info,
                        'timestamp': current_time,
                        'is_live': info.get('is_live', False)
                    }
                    logger.info(f"Video info cached. Is live: {info.get('is_live', False)}")
                    return info
                else:
                    logger.error("Failed to get video info")
                    return None

        except Exception as e:
            logger.error(f"Error getting video info: {e}")
            # If we have stale cache, try using it
            if video_id in self._video_info_cache:
                logger.warning("Using stale cache due to error")
                return self._video_info_cache[video_id]['info']
            return None

    def extract_audio_chunk_from_video(self, video_id: str, start_time: float, duration: float, force_refresh_info: bool = False) -> Optional[str]:
        """
        Extract a specific audio chunk from a non-live video using yt-dlp and ffmpeg.

        Args:
            video_id: YouTube video ID or URL (for Twitter/X.com support)
            start_time: Start time in seconds
            duration: Duration in seconds to extract
            force_refresh_info: Force refresh of cached video info

        Returns:
            Path to extracted audio file, or None if extraction failed
        """
        try:
            import tempfile

            import yt_dlp

            # Determine URL: local file, full URL, or YouTube video ID
            if _is_local_file(video_id):
                url = video_id
                url_hash = str(hash(video_id))[:8]
                audio_filename = f"local_audio_{url_hash}_{int(start_time)}_{int(time.time())}.wav"
            elif video_id.startswith("http://") or video_id.startswith("https://"):
                url = video_id
                url_hash = str(hash(video_id))[:8]
                audio_filename = f"video_audio_{url_hash}_{int(start_time)}_{int(time.time())}.wav"
            else:
                url = f"https://www.youtube.com/watch?v={video_id}"
                audio_filename = f"youtube_audio_{video_id}_{int(start_time)}_{int(time.time())}.wav"

            # Create temporary file for audio
            temp_dir = tempfile.gettempdir()
            audio_path = os.path.join(temp_dir, audio_filename)

            # Get video info (uses cache if available)
            info = self._get_video_info(video_id, force_refresh=force_refresh_info)

            if not info:
                logger.error("Failed to get video info")
                return None

            # Use yt-dlp with external downloader (ffmpeg) to extract specific time range
            # Note: When using external_downloader, yt-dlp might save the file with a different name
            # We'll search for the file after download
            base_audio_path = audio_path.replace('.wav', '')
            ydl_opts_chunk = {
                'format': 'bestaudio/best',
                'outtmpl': base_audio_path + '.%(ext)s',  # Let yt-dlp choose the extension
                'external_downloader': 'ffmpeg',
                'external_downloader_args': [
                    '-ss', str(start_time),  # Start time
                    '-t', str(duration),     # Duration
                    '-loglevel', 'error'
                ],
                'postprocessors': [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'wav',
                    'preferredquality': '192',
                }],
                'quiet': True,
                'no_warnings': True,
                'http_headers': {
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                    'Accept-Language': 'en-us,en;q=0.5',
                    'Accept-Encoding': 'gzip, deflate',
                    'Connection': 'keep-alive',
                },
            }

            try:
                # Check shutdown flag before download
                if self._check_shutdown():
                    logger.debug("Shutdown requested before audio download")
                    self._cleanup_temp_files(base_audio_path)
                    return None

                logger.info(f"Downloading audio chunk from {start_time}s for {duration}s (video_id: {video_id})")
                logger.debug(f"Base audio path: {base_audio_path}")

                # Download the audio chunk
                with yt_dlp.YoutubeDL(ydl_opts_chunk) as ydl:
                    try:
                        # Download directly - extract_info might interfere with external_downloader
                        ydl.download([url])
                        logger.debug("yt-dlp download completed successfully")
                    except Exception as download_error:
                        logger.error(f"yt-dlp download failed: {download_error}", exc_info=True)
                        # Check if it's a known error that we can retry
                        error_str = str(download_error).lower()
                        if 'http' in error_str or 'network' in error_str or 'timeout' in error_str:
                            logger.warning("Network-related error, might be temporary")
                        return None

                    # Check shutdown flag after download
                    if self._check_shutdown():
                        logger.debug("Shutdown requested after audio download")
                        self._cleanup_temp_files(base_audio_path)
                        return None

                    # Find the actual audio file with retry logic
                    # yt-dlp might have saved it with a different name, so we search broadly
                    max_retries = 5
                    retry_delay = 0.2

                    logger.debug(f"Searching for audio file with base path: {base_audio_path}")
                    found_files = []
                    for retry in range(max_retries):
                        # Check shutdown flag
                        if self._check_shutdown():
                            logger.debug("Shutdown requested during audio file search")
                            self._cleanup_temp_files(base_audio_path)
                            return None

                        # List all files in temp directory that match our pattern
                        temp_dir = os.path.dirname(base_audio_path)
                        base_name = os.path.basename(base_audio_path)
                        if os.path.exists(temp_dir):
                            all_files = os.listdir(temp_dir)
                            # Look for files that start with our base name
                            matching_files = [f for f in all_files if f.startswith(base_name)]
                            if matching_files:
                                logger.debug(f"Found potential audio files matching base name: {matching_files}")

                        # Try the expected extensions first
                        for ext in ['.wav', '.m4a', '.webm', '.opus', '.mp3']:
                            candidate = base_audio_path + ext
                            if os.path.exists(candidate):
                                file_size = os.path.getsize(candidate)
                                logger.debug(f"Found candidate file: {candidate} (size: {file_size} bytes)")
                                found_files.append((candidate, file_size))
                                # Wait a bit to ensure file is fully written
                                time.sleep(0.1)
                                # Verify file is not empty
                                if file_size > 0:
                                    logger.info(f"Successfully found audio file: {candidate} ({file_size} bytes)")
                                    return candidate
                                else:
                                    logger.warning(f"Found file but it's empty: {candidate}")

                        if retry < max_retries - 1:
                            logger.debug(f"Audio file not found yet, retry {retry + 1}/{max_retries}")
                            # Make sleep interruptible
                            sleep_chunk = 0.1
                            slept = 0
                            while slept < retry_delay and not self._check_shutdown():
                                remaining = min(sleep_chunk, retry_delay - slept)
                                time.sleep(remaining)
                                slept += remaining
                            retry_delay *= 1.5  # Exponential backoff

                    # Log detailed information about what we found
                    if found_files:
                        logger.warning(f"Audio file not found after download, but found these files: {found_files}")
                    else:
                        logger.warning(f"Audio file not found after download and retries. Base path: {base_audio_path}")
                        # List temp directory contents for debugging
                        temp_dir = os.path.dirname(base_audio_path)
                        if os.path.exists(temp_dir):
                            recent_files = [f for f in os.listdir(temp_dir) if 'youtube_audio' in f]
                            if recent_files:
                                logger.debug(f"Recent youtube_audio files in temp dir: {recent_files[-10:]}")

                            # Also check for files modified in the last minute (might be our download)
                            import time as time_module
                            current_time = time_module.time()
                            very_recent_files = []
                            for f in os.listdir(temp_dir):
                                fpath = os.path.join(temp_dir, f)
                                if os.path.isfile(fpath):
                                    mtime = os.path.getmtime(fpath)
                                    if current_time - mtime < 60:  # Modified in last minute
                                        very_recent_files.append((f, current_time - mtime))
                            if very_recent_files:
                                logger.debug(f"Very recently modified files in temp dir: {very_recent_files}")
                    return None
            except Exception as e:
                logger.error(f"Error downloading audio chunk from {start_time}s: {e}", exc_info=True)
                import traceback
                logger.debug(f"Full traceback: {traceback.format_exc()}")
                return None

        except ImportError:
            logger.error("yt-dlp not installed. Install with: pip install yt-dlp")
            return None
        except Exception as e:
            logger.error(f"Error extracting audio chunk: {e}")
            import traceback
            logger.debug(traceback.format_exc())
            return None

    def extract_audio_from_youtube(self, video_id: str, duration: int = 30, overlap: int = 0, start_time: Optional[float] = None, force_refresh_info: bool = False) -> Optional[str]:
        """
        Extract audio from YouTube video using yt-dlp.

        Args:
            video_id: YouTube video ID or URL (for Twitter/X.com support)
            duration: Duration in seconds to extract (for live streams, this is the chunk size)
            overlap: Overlap in seconds with previous chunk (default: 0)
            start_time: Start time in seconds (for non-live videos, if None, downloads entire video)
            force_refresh_info: Force refresh of cached video info

        Returns:
            Path to extracted audio file, or None if extraction failed
        """
        try:
            import tempfile
            import threading

            import yt_dlp

            # Determine URL: local file, full URL, or YouTube video ID
            if _is_local_file(video_id):
                url = video_id
                url_hash = str(hash(video_id))[:8]
                audio_filename = f"local_audio_{url_hash}_{int(time.time())}.wav"
            elif video_id.startswith("http://") or video_id.startswith("https://"):
                url = video_id
                url_hash = str(hash(video_id))[:8]
                audio_filename = f"video_audio_{url_hash}_{int(time.time())}.wav"
            else:
                url = f"https://www.youtube.com/watch?v={video_id}"
                audio_filename = f"youtube_audio_{video_id}_{int(time.time())}.wav"

            # Create temporary file for audio
            temp_dir = tempfile.gettempdir()
            audio_path = os.path.join(temp_dir, audio_filename)

            # Get video info (uses cache if available)
            info = self._get_video_info(video_id, force_refresh=force_refresh_info)

            if not info:
                logger.error("Failed to get video info")
                return None

            # Check if it's a live stream
            is_live = info.get('is_live', False)
            logger.debug(f"Is live stream: {is_live}")

            # For live streams, use yt-dlp with duration limit
            # This is more reliable than trying to use ffmpeg with stream URLs
            if is_live:
                # For overlap: download (duration + overlap) seconds, but we'll track the effective duration
                # The overlap ensures continuous coverage without gaps
                download_duration = duration + overlap
                logger.info(f"Live stream detected, extracting {duration}s audio chunk (with {overlap}s overlap = {download_duration}s total)...")
                # Use yt-dlp with external downloader (ffmpeg) to limit duration
                ydl_opts_live = {
                    'format': 'bestaudio/best',
                    'outtmpl': audio_path.replace('.wav', '.%(ext)s'),
                    'external_downloader': 'ffmpeg',
                    'external_downloader_args': ['-t', str(download_duration), '-loglevel', 'error'],  # Download duration + overlap
                    'postprocessors': [{
                        'key': 'FFmpegExtractAudio',
                        'preferredcodec': 'wav',
                        'preferredquality': '192',
                    }],
                    'quiet': True,  # Suppress yt-dlp output
                    'no_warnings': True,
                    # Add headers to avoid 403 errors
                    'http_headers': {
                        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                        'Accept-Language': 'en-us,en;q=0.5',
                        'Accept-Encoding': 'gzip, deflate',
                        'Connection': 'keep-alive',
                    },
                }

                try:
                    # Calculate timeout: use max of (download_duration * 2) or 45 seconds
                    # This ensures short intervals have enough time for connection overhead
                    timeout_seconds = max(download_duration * 2, 45)
                    logger.info(f"Downloading {download_duration}s audio chunk (this may take up to {timeout_seconds}s)...")
                    with yt_dlp.YoutubeDL(ydl_opts_live) as ydl:
                        # Set a timeout for the download
                        download_complete = threading.Event()
                        download_error = [None]

                        def download_thread():
                            try:
                                ydl.download([url])
                                download_complete.set()
                            except Exception as e:
                                download_error[0] = e
                                download_complete.set()

                        thread = threading.Thread(target=download_thread)
                        thread.daemon = True
                        thread.start()

                        # Wait with timeout (adaptive based on duration)
                        if download_complete.wait(timeout=timeout_seconds):
                            if download_error[0]:
                                # On error, invalidate cache and try once more
                                if not force_refresh_info and video_id in self._video_info_cache:
                                    logger.warning("Download error, invalidating cache and retrying...")
                                    del self._video_info_cache[video_id]
                                    # Retry with fresh info (but limit to one retry to avoid infinite loop)
                                    return self.extract_audio_from_youtube(video_id, duration, overlap=overlap, start_time=start_time, force_refresh_info=True)
                                raise download_error[0]
                        else:
                            logger.warning(f"Download timeout after {timeout_seconds}s")
                            self._cleanup_temp_files(audio_path.replace('.wav', ''))
                            return None

                        # Find the actual audio file
                        logger.debug("Searching for downloaded audio file...")
                        base_path = audio_path.replace('.wav', '')
                        for ext in ['.wav', '.m4a', '.webm', '.opus', '.mp3']:
                            candidate = base_path + ext
                            if os.path.exists(candidate):
                                logger.debug(f"Found audio file: {candidate}")
                                return candidate

                        logger.warning("Audio file not found after download")
                        return None

                except Exception as e:
                    logger.error(f"Error downloading live stream chunk: {e}")
                    # Invalidate cache on persistent errors
                    if video_id in self._video_info_cache:
                        logger.debug("Invalidating cache due to persistent error")
                        del self._video_info_cache[video_id]
                    return None
            else:
                # For non-live videos, if start_time is provided, extract chunk
                if start_time is not None:
                    logger.debug(f"Extracting chunk from {start_time}s for {duration}s")
                    return self.extract_audio_chunk_from_video(video_id, start_time, duration, force_refresh_info)

                # Otherwise, download entire video (for full processing mode)
                logger.info("Non-live video, downloading entire audio...")
                ydl_opts_download = {
                    'format': 'bestaudio/best',
                    'outtmpl': audio_path.replace('.wav', '.%(ext)s'),
                    'postprocessors': [{
                        'key': 'FFmpegExtractAudio',
                        'preferredcodec': 'wav',
                        'preferredquality': '192',
                    }],
                    'quiet': True,
                    'no_warnings': True,
                    # Add headers to avoid 403 errors
                    'http_headers': {
                        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                        'Accept-Language': 'en-us,en;q=0.5',
                        'Accept-Encoding': 'gzip, deflate',
                        'Connection': 'keep-alive',
                    },
                }

                try:
                    with yt_dlp.YoutubeDL(ydl_opts_download) as ydl:
                        logger.info("Downloading audio...")
                        ydl.download([url])

                        # Find the actual audio file (yt-dlp may change extension)
                        logger.info("Searching for downloaded audio file...")
                        base_path = audio_path.replace('.wav', '')
                        for ext in ['.wav', '.m4a', '.webm', '.opus', '.mp3']:
                            candidate = base_path + ext
                            if os.path.exists(candidate):
                                logger.info(f"Found audio file: {candidate}")
                                return candidate

                        logger.warning("Audio file not found after download")
                        return None
                except Exception as e:
                    logger.error(f"Error downloading audio: {e}")
                    return None

        except ImportError:
            logger.error("yt-dlp not installed. Install with: pip install yt-dlp")
            return None
        except Exception as e:
            logger.error(f"Error extracting audio: {e}")
            import traceback
            logger.debug(traceback.format_exc())
            return None

    def _detect_language(self, audio_path: str) -> Tuple[str, float]:
        """
        Quickly detect the language of audio using tiny Whisper model.

        Args:
            audio_path: Path to audio file

        Returns:
            Tuple of (detected_language, language_probability)
        """
        try:
            if self.language_detector is None:
                return ("unknown", 0.0)

            if self.use_faster_whisper:
                _, info = self.language_detector.transcribe(audio_path, beam_size=1, vad_filter=True)
                detected_language = info.language if hasattr(info, 'language') else "unknown"
                language_probability = info.language_probability if hasattr(info, 'language_probability') else 0.0
            else:
                result = self.language_detector.transcribe(audio_path)
                detected_language = result.get("language", "unknown")
                language_probability = 1.0

            return (detected_language, language_probability)
        except Exception as e:
            logger.debug(f"Error detecting language: {e}")
            return ("unknown", 0.0)

    def transcribe_audio(self, audio_path: str) -> Tuple[str, str, float, list]:
        """
        Transcribe audio file using Whisper.
        Uses tiny model for English, small model for other languages.

        Args:
            audio_path: Path to audio file

        Returns:
            Tuple of (transcribed_text, detected_language, language_probability, segments).
            `segments` is a list of dicts {'start': float, 'end': float, 'text': str}
            with times relative to the start of `audio_path` (chunk-relative when the
            caller passes a chunk; pipeline code is responsible for offsetting to
            video-absolute time). May be an empty list when segment-level timing is
            unavailable (e.g. openai-whisper's base `transcribe` without
            `word_timestamps`, or on shutdown before any segment arrives).
        """
        if not os.path.exists(audio_path):
            logger.error(f"Audio file not found: {audio_path}")
            return ("", "unknown", 0.0, [])

        try:
            # First, quickly detect language using tiny model
            detected_language, lang_prob = self._detect_language(audio_path)
            logger.debug(f"Detected language: {detected_language} (probability: {lang_prob:.2f})")

            # Get appropriate model based on language
            model = self._get_transcription_model(detected_language)
            model_size = "tiny" if detected_language == "en" else "small"
            logger.info(f"Using {model_size} model for {detected_language} transcription")

            # Transcribe with appropriate model
            if self.use_faster_whisper:
                # Use faster-whisper
                segments, info = model.transcribe(audio_path, beam_size=5)

                # Collect all text segments plus structured timing
                text_parts = []
                segment_records = []
                for segment in segments:
                    # Check shutdown flag during transcription
                    if self._check_shutdown():
                        logger.debug("Shutdown requested during transcription, stopping...")
                        break
                    text_parts.append(segment.text)
                    segment_records.append({
                        'start': float(getattr(segment, 'start', 0.0) or 0.0),
                        'end': float(getattr(segment, 'end', 0.0) or 0.0),
                        'text': segment.text,
                    })

                text = " ".join(text_parts).strip()
                # Use detected language from language detector if available, otherwise from transcription
                if detected_language != "unknown":
                    final_language = detected_language
                    final_prob = lang_prob
                else:
                    final_language = info.language if hasattr(info, 'language') else "unknown"
                    final_prob = info.language_probability if hasattr(info, 'language_probability') else 0.0

                return (text, final_language, final_prob, segment_records)
            else:
                # Use openai-whisper
                result = model.transcribe(audio_path)
                text = result["text"].strip()

                # openai-whisper returns segment-level timing in result["segments"]
                segment_records = []
                for seg in result.get("segments", []) or []:
                    seg_text = seg.get("text", "")
                    if seg_text is None:
                        continue
                    segment_records.append({
                        'start': float(seg.get('start', 0.0) or 0.0),
                        'end': float(seg.get('end', 0.0) or 0.0),
                        'text': seg_text,
                    })

                # Use detected language from language detector if available, otherwise from transcription
                if detected_language != "unknown":
                    final_language = detected_language
                    final_prob = lang_prob
                else:
                    final_language = result.get("language", "unknown")
                    final_prob = 1.0

                return (text, final_language, final_prob, segment_records)

        except Exception as e:
            logger.error(f"Error transcribing audio: {e}")
            return ("", "unknown", 0.0, [])
        finally:
            # Clean up audio file
            try:
                if os.path.exists(audio_path):
                    os.remove(audio_path)
            except OSError as e:
                logger.debug("Could not remove temp audio file %s: %s", audio_path, e)

    def translate_to_russian(self, text: str, source_language: str) -> str:
        """
        Translate text to Russian if source language is not Russian.
        Translates in chunks to avoid API limits.

        Args:
            text: Text to translate
            source_language: Source language code (e.g., 'en', 'ru', 'es')

        Returns:
            Translated text (or original if already Russian or translation fails)
        """
        if not text.strip():
            return text

        # If already Russian, return as-is
        if source_language == 'ru':
            self._last_detected_language = 'ru'
            return text

        try:
            from deep_translator import GoogleTranslator

            # Cache translator instance by source language
            if source_language not in self._translator_cache:
                logger.debug(f"Creating translator for {source_language} -> ru")
                self._translator_cache[source_language] = GoogleTranslator(source=source_language, target='ru')

            translator = self._translator_cache[source_language]
            self._last_detected_language = source_language

            # Split text into chunks (max ~5000 characters per chunk to avoid API limits)
            max_chunk_size = 4500  # Conservative limit
            text_chunks = []

            if len(text) <= max_chunk_size:
                text_chunks = [text]
            else:
                # Split by sentences first, then by chunks
                sentences = re.split(r'([.!?]\s+)', text)
                current_chunk = ""

                for i in range(0, len(sentences), 2):
                    sentence = sentences[i] + (sentences[i+1] if i+1 < len(sentences) else "")

                    if len(current_chunk) + len(sentence) <= max_chunk_size:
                        current_chunk += sentence
                    else:
                        if current_chunk:
                            text_chunks.append(current_chunk)
                        # If single sentence is too long, split by words
                        if len(sentence) > max_chunk_size:
                            words = sentence.split()
                            temp_chunk = ""
                            for word in words:
                                if len(temp_chunk) + len(word) + 1 <= max_chunk_size:
                                    temp_chunk += word + " "
                                else:
                                    if temp_chunk:
                                        text_chunks.append(temp_chunk.strip())
                                    temp_chunk = word + " "
                            if temp_chunk:
                                current_chunk = temp_chunk
                        else:
                            current_chunk = sentence

                if current_chunk:
                    text_chunks.append(current_chunk)

            # Translate each chunk
            translated_chunks = []
            for i, chunk in enumerate(text_chunks):
                try:
                    logger.debug(f"Translating chunk {i+1}/{len(text_chunks)} ({len(chunk)} chars)...")
                    translated_chunk = translator.translate(chunk)
                    if translated_chunk:
                        translated_chunks.append(translated_chunk)
                    else:
                        logger.warning(f"Translation chunk {i+1} returned empty, using original")
                        translated_chunks.append(chunk)
                except Exception as e:
                    logger.warning(f"Translation failed for chunk {i+1}: {e}, using original")
                    translated_chunks.append(chunk)

            translated = " ".join(translated_chunks)

            if translated:
                logger.debug(f"Translation successful ({len(text_chunks)} chunks)")
                return translated
            else:
                logger.warning("Translation returned empty, using original text")
                return text

        except ImportError:
            logger.error("deep-translator not installed. Install with: pip install deep-translator")
            return text
        except Exception as e:
            logger.warning(f"Translation failed: {e}, using original text")
            return text

    def translate_segments_to_russian(
        self,
        segments: list,
        source_language: str,
        batch_size: int = 10,
    ) -> Tuple[str, list]:
        """
        Translate a list of whisper segments to Russian while preserving
        per-segment alignment. Used by the pipeline to build anchors for
        translated text: the returned `translated_segments` carry the same
        `start`/`end` times as the originals, with `text` replaced by the
        Russian translation.

        Batching strategy: segments are joined with "\\n|||\\n" and sent in
        groups of `batch_size` to Google Translate. After translation the
        response is split on `|||`. If the split count does not match the
        batch size (separator lost, Google translated it, etc.) we fall back
        to per-segment translation for that batch. Worst case: one call per
        segment; typical case: one call per 10 segments.

        Args:
            segments: list of {'start', 'end', 'text'} dicts (chunk-relative times).
            source_language: source language code (e.g. 'en'). 'ru' -> no-op.
            batch_size: number of segments joined per Google call.

        Returns:
            (joined_russian_text, translated_segments).
            `joined_russian_text` = ' '.join(s['text'] for s in translated_segments).strip()
            — same shape as the existing translate_to_russian result.
            `translated_segments` is a list with the same length as input
            (or possibly shorter if empty segments were filtered), each carrying
            the same start/end plus Russian 'text'.
        """
        if not segments:
            return '', []

        # Trivial no-op for already-Russian content.
        if source_language == 'ru':
            self._last_detected_language = 'ru'
            # Return deep copies so callers can mutate without surprising us.
            passthrough = [dict(s) for s in segments]
            joined = " ".join(s.get('text', '') for s in passthrough).strip()
            return joined, passthrough

        try:
            from deep_translator import GoogleTranslator

            if source_language not in self._translator_cache:
                logger.debug(f"Creating translator for {source_language} -> ru")
                self._translator_cache[source_language] = GoogleTranslator(source=source_language, target='ru')
            translator = self._translator_cache[source_language]
            self._last_detected_language = source_language

            # Defensive: neutralize any literal '|||' that might appear in source
            # text so our batch separator stays unique. Practically unheard-of
            # in natural speech, but free insurance.
            def _sanitize(t: str) -> str:
                return (t or '').replace('|||', '| | |')

            translated_segments: list = []

            # Split input into batches
            for start in range(0, len(segments), batch_size):
                batch = segments[start:start + batch_size]
                batch_texts = [_sanitize(s.get('text', '')) for s in batch]

                # If the whole batch is empty, just carry originals through
                if not any(t.strip() for t in batch_texts):
                    translated_segments.extend(dict(s) for s in batch)
                    continue

                joined_input = "\n|||\n".join(batch_texts)

                def _per_segment_fallback(batch_slice):
                    """Translate each segment individually; on failure keep original."""
                    out = []
                    for orig in batch_slice:
                        src = _sanitize(orig.get('text', ''))
                        if not src.strip():
                            out.append(dict(orig))
                            continue
                        try:
                            single = translator.translate(src)
                            if single:
                                out.append({**orig, 'text': single.strip()})
                            else:
                                out.append(dict(orig))
                        except Exception as inner_e:
                            logger.warning(
                                f"Per-segment translation failed (start={orig.get('start')}): {inner_e}"
                            )
                            out.append(dict(orig))
                    return out

                try:
                    response = translator.translate(joined_input) or ""
                except Exception as e:
                    logger.warning(f"Batch translate raised {e!r}; falling back per-segment")
                    translated_segments.extend(_per_segment_fallback(batch))
                    continue

                # Split on '|||' (ignoring surrounding whitespace/newlines — Google
                # may reformat them).
                pieces = [p.strip() for p in response.split('|||')]

                if len(pieces) != len(batch):
                    logger.warning(
                        f"Batch separator lost ({len(pieces)} pieces vs {len(batch)} "
                        f"segments); falling back per-segment"
                    )
                    translated_segments.extend(_per_segment_fallback(batch))
                    continue

                for orig, translated_piece in zip(batch, pieces):
                    if translated_piece:
                        translated_segments.append({**orig, 'text': translated_piece})
                    else:
                        # Empty Russian for this slot — keep original text as fallback.
                        translated_segments.append(dict(orig))

            joined = " ".join(s.get('text', '') for s in translated_segments).strip()
            logger.debug(
                f"Segment translation complete: {len(segments)} segments -> "
                f"{len(translated_segments)} translated ({len(joined)} chars)"
            )
            return joined, translated_segments

        except ImportError:
            logger.error("deep-translator not installed. Install with: pip install deep-translator")
            passthrough = [dict(s) for s in segments]
            joined = " ".join(s.get('text', '') for s in passthrough).strip()
            return joined, passthrough
        except Exception as e:
            logger.warning(f"Segment translation setup failed: {e}; returning original text")
            passthrough = [dict(s) for s in segments]
            joined = " ".join(s.get('text', '') for s in passthrough).strip()
            return joined, passthrough

