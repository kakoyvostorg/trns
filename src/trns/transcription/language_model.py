"""
Language Model Processor Module

Processes Russian transcriptions through a language model (HuggingFace) to generate reports.
Runs asynchronously in a background worker thread.
"""

import logging
import math
import queue
from datetime import datetime
from typing import Callable, Dict, List, Optional

# Imports use package structure

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Timecode helpers (module-level so they're easy to unit-test)
# ---------------------------------------------------------------------------

# Bilingual instruction appended to prompts when timecode_in_summary is on.
# Short on purpose: every extra token on every LM call adds cost.
_TIMECODE_INSTRUCTION_EN = (
    "The transcript below contains inline time markers in the form [MM:SS] "
    "or [HH:MM:SS] indicating where each span starts in the source video. "
    "When your summary refers to a specific moment, cite the nearest marker."
)
_TIMECODE_INSTRUCTION_RU = (
    "В транскрипте ниже встречаются метки времени вида [MM:SS] или "
    "[HH:MM:SS], показывающие, где начинается соответствующий фрагмент в "
    "исходном видео. Если в кратком пересказе вы ссылаетесь на конкретный "
    "момент, приведите ближайшую метку."
)


def _format_timecode_for_prompt(seconds: float, force_hours: bool = False) -> str:
    """Bracketed [MM:SS] / [HH:MM:SS] formatter used inside the serializer.

    Kept local to this module so language_model.py doesn't import from the
    pipeline module — that would be a circular import risk. The logic mirrors
    `pipeline._format_timecode` exactly; both exist so the two layers stay
    independent.
    """
    if seconds is None:
        total = 0
    else:
        try:
            total = int(seconds)
        except (TypeError, ValueError):
            total = 0
    if total < 0:
        total = 0
    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60
    if force_hours or total >= 3600:
        return f"[{h:02d}:{m:02d}:{s:02d}]"
    return f"[{m:02d}:{s:02d}]"


def _serialize_window_with_timecodes(
    transcriptions: List[Dict],
    which: str = 'translated',
    min_interval_seconds: float = 30.0,
    force_hours: Optional[bool] = None,
) -> str:
    """
    Join a window of transcription dicts into one text string, injecting
    sparse [MM:SS] markers at roughly `min_interval_seconds` spacing. Anchor
    times are in video-absolute seconds (the pipeline guarantees this).

    Sparseness policy: the first anchor of the window always emits a marker;
    subsequent anchors only emit when their time is at least
    `min_interval_seconds` after the most-recently emitted marker. This keeps
    the prompt from ballooning on long videos while still giving the model
    enough reference points to cite moments.

    Args:
        transcriptions: list of transcription dicts. Each may have 'anchors'
            (paired with 'text') or 'anchors_translated' (paired with
            'translated'). Dicts missing anchors are emitted as plain text
            with no markers — they still contribute to the joined string.
        which: 'translated' uses ('translated', 'anchors_translated');
               'text' uses ('text', 'anchors').
        min_interval_seconds: minimum spacing (in video seconds) between
            emitted markers. Must be >= 0; 0 means "emit every anchor".
        force_hours: if True, every marker uses [HH:MM:SS]. None = auto
            (the per-marker threshold at 3600s still applies). When serialized
            content crosses the 1-hour line, callers can pass True to keep
            widths uniform.

    Returns:
        A single string. Empty string when the window has no emittable text.
    """
    if which == 'translated':
        text_key, anchor_key = 'translated', 'anchors_translated'
    elif which == 'text':
        text_key, anchor_key = 'text', 'anchors'
    else:
        raise ValueError(f"which must be 'translated' or 'text', got {which!r}")

    if not transcriptions:
        return ''

    min_interval = max(0.0, float(min_interval_seconds))

    out_parts: List[str] = []
    marker_count = 0
    # Track the last emitted marker time across the whole window so spacing
    # holds across transcription boundaries, not just within each dict.
    last_emitted_time: Optional[float] = None

    for t in transcriptions:
        piece = (t.get(text_key) or '').strip()
        if not piece:
            continue
        anchors = t.get(anchor_key) or []

        if not anchors:
            # No timing info for this dict — emit text as-is.
            out_parts.append(piece)
            continue

        # Walk the piece by anchor offsets. Anchors are already {'offset','time'}
        # into this dict's text; we rebuild the dict's string with markers
        # inserted at chosen anchor points, then append to out_parts.
        rebuilt: List[str] = []
        prev_offset = 0
        for anc in anchors:
            off = anc.get('offset', 0)
            t_abs = anc.get('time', 0.0)
            if off < prev_offset or off > len(piece):
                # Defensive: malformed anchor — skip without exploding.
                continue
            # Decide whether this anchor crosses the sparse threshold.
            if (
                last_emitted_time is None
                or (t_abs - last_emitted_time) >= min_interval
            ):
                # Flush text up to this anchor, then drop the marker.
                if off > prev_offset:
                    rebuilt.append(piece[prev_offset:off])
                marker = _format_timecode_for_prompt(
                    t_abs,
                    force_hours=bool(force_hours),
                )
                # Leading space keeps markers visually separated from prior
                # text; it gets normalized by downstream .strip()/join logic.
                rebuilt.append(f" {marker} ")
                marker_count += 1
                last_emitted_time = t_abs
                prev_offset = off
            # else: anchor is too close to previous marker — skip emission.
        # Flush the remainder of the piece.
        if prev_offset < len(piece):
            rebuilt.append(piece[prev_offset:])
        out_parts.append(''.join(rebuilt).strip())

    # Join the per-dict pieces with a space; the serializer never introduces
    # extra newlines so downstream LM prompt formatting stays predictable.
    serialized = ' '.join(p for p in out_parts if p).strip()
    logger.debug(
        "Serialized transcript window with %d timecode markers using %s anchors",
        marker_count,
        anchor_key,
    )
    return serialized


class LMProcessor:
    """
    Processes Russian transcriptions through a language model.
    Maintains a sliding window of transcriptions and generates reports.
    """

    def __init__(
        self,
        api_key_file: str = "api_key.txt",
        prompt_file: str = "prompt.md",
        prompt_original_file: str = "prompt_original.md",
        model: str = "google/gemma-3-27b-it:free",
        window_seconds: int = 120,
        interval: int = 30,
        context: str = "",
        shutdown_flag: Optional[Callable[[], bool]] = None,
        use_bilingual: bool = False,
        detected_language: str = "ru",
        metadata_path: str = "metadata.json",
        capacity_path: Optional[str] = None,
        video_metadata_prefix: str = "",
        timecode_in_summary: bool = False,
        timecode_min_interval_seconds: float = 30.0,
    ):
        """
        Initialize LM processor.

        Args:
            api_key_file: Path to file containing OpenRouter.ai API key
            prompt_file: Path to file containing the Russian prompt
            prompt_original_file: Path to file containing the original language prompt template
            model: OpenRouter.ai model name
            window_seconds: Window size n for LM processing (default: 120)
            interval: Interval between transcriptions in seconds (default: 30)
            context: Additional context to add to prompt (default: empty string)
            shutdown_flag: Optional callable to check for shutdown signal
            use_bilingual: Whether to process both original and Russian (default: False)
            detected_language: Detected language code (default: "ru")
            metadata_path: Path to metadata.json for token management
            capacity_path: Path to mutable daily-capacity state. Defaults to
                trns.bot.utils' runtime state/global.json.
            video_metadata_prefix: Optional prompt prefix containing the video's
                title and description. Prepended to the user text in each LM
                call. Expected to already include any "<video_metadata>" wrapper
                and a trailing blank line. Pass an empty string to disable.
            timecode_in_summary: When True, the transcript window sent to the
                LM has sparse [MM:SS] markers inserted at anchor points, and a
                short bilingual instruction is appended to the prompt telling
                the model what the markers mean. Default False leaves behaviour
                identical to pre-feature code.
            timecode_min_interval_seconds: Spacing between emitted markers,
                measured in source-video seconds. Lower = more markers, more
                tokens. Default 30s matches the typical chunk cadence.
        """
        self.api_key_file = api_key_file
        self.prompt_file = prompt_file
        self.prompt_original_file = prompt_original_file
        self.metadata_path = metadata_path
        self.capacity_path = capacity_path
        self.model = model
        self.window_seconds = window_seconds
        self.interval = interval
        self.context = context
        self.shutdown_flag = shutdown_flag
        self.use_bilingual = use_bilingual
        self.detected_language = detected_language
        self.video_metadata_prefix = video_metadata_prefix or ""
        self.timecode_in_summary = bool(timecode_in_summary)
        self.timecode_min_interval_seconds = float(timecode_min_interval_seconds)

        # Initialize OpenAI client
        self.client = None
        self.prompt_russian = None
        self.prompt_original_template = None
        self._initialize_client()
        self._load_prompts()

    def _check_shutdown(self) -> bool:
        """Check if shutdown was requested"""
        if self.shutdown_flag is None:
            return False
        if callable(self.shutdown_flag):
            return self.shutdown_flag()
        return bool(self.shutdown_flag)

    def _initialize_client(self):
        """Initialize OpenAI client with OpenRouter.ai using token management"""
        try:
            from openai import OpenAI

            # Try to import token management utilities
            try:
                from trns.bot.utils import (
                    get_current_token,
                    get_text,
                    load_metadata,
                )
                use_token_management = True
            except ImportError:
                # Fallback to old method if utils not available
                use_token_management = False
                logger.warning("Token management utilities not available, using direct file read")

            if use_token_management:
                # Use token management system
                api_key = get_current_token(self.api_key_file, self.metadata_path)
                if api_key is None:
                    # Try to load metadata for error message
                    try:
                        metadata = load_metadata(self.metadata_path)
                        error_msg = get_text(metadata, "no_tokens_available")
                        logger.error(error_msg)
                    except Exception:
                        logger.error("No tokens available in api_key.txt")
                    raise ValueError("No tokens available")

                # Check capacity warning (capacity is checked at start of processing, not here)
                # This is just for logging during initialization
            else:
                # Fallback: read first token from file
                with open(self.api_key_file, "r", encoding="utf-8") as f:
                    api_key = f.read().strip().split('\n')[0]

            self.client = OpenAI(
                base_url="https://openrouter.ai/api/v1",
                api_key=api_key,
            )
            logger.info("OpenAI client (OpenRouter.ai) initialized")
        except FileNotFoundError:
            logger.error(f"API key file not found: {self.api_key_file}")
            raise
        except ImportError:
            logger.error("openai not installed. Install with: pip install openai")
            raise
        except Exception as e:
            logger.error(f"Error initializing OpenAI client: {e}")
            raise

    def _load_prompts(self):
        """Load prompt files"""
        try:
            # Load Russian prompt
            with open(self.prompt_file, "r", encoding="utf-8") as f:
                self.prompt_russian = f.read()

            # Add context to Russian prompt if provided
            if self.context and self.context.strip():
                self.prompt_russian += f"\nДополнительный контекст: {self.context}"
                logger.info(f"Russian prompt loaded from {self.prompt_file} with additional context")
            else:
                logger.info(f"Russian prompt loaded from {self.prompt_file}")

            # Load original language prompt template
            with open(self.prompt_original_file, "r", encoding="utf-8") as f:
                self.prompt_original_template = f.read()

            # Add context to original template if provided
            if self.context and self.context.strip():
                self.prompt_original_template += f"\nAdditional context: {self.context}"
                logger.info(f"Original language prompt template loaded from {self.prompt_original_file} with additional context")
            else:
                logger.info(f"Original language prompt template loaded from {self.prompt_original_file}")

            # When timecode-in-summary is enabled, append the bilingual
            # instruction block once so every request carries the rubric. This
            # is re-run when _load_prompts is re-invoked after language
            # detection; that's fine — it's appended to a fresh file read.
            if self.timecode_in_summary:
                self.prompt_russian = (
                    (self.prompt_russian or "").rstrip()
                    + "\n\n" + _TIMECODE_INSTRUCTION_RU
                )
                self.prompt_original_template = (
                    (self.prompt_original_template or "").rstrip()
                    + "\n\n" + _TIMECODE_INSTRUCTION_EN
                )

        except FileNotFoundError as e:
            logger.error(f"Prompt file not found: {e}")
            raise
        except Exception as e:
            logger.error(f"Error loading prompts: {e}")
            raise

    def _calculate_window_size(self) -> int:
        """
        Calculate the number of transcriptions to include in the window.
        Returns ceil(window_seconds / interval)
        """
        return math.ceil(self.window_seconds / self.interval)

    def _get_window_text(self, all_transcriptions: List[Dict], which: str = 'translated') -> str:
        """
        Extract the last N transcriptions (where N = ceil(window_seconds / interval))
        and concatenate their text. Passes through the timecode serializer
        when `self.timecode_in_summary` is enabled.

        Args:
            all_transcriptions: List of transcription dicts with 'translated' key
            which: 'translated' (default) returns Russian; 'text' returns the
                original-language text. Under timecode-in-summary mode this
                also selects the matching anchors key.

        Returns:
            Concatenated text from the window (plus inline markers when
            timecode-in-summary is on).
        """
        if not all_transcriptions:
            return ""

        window_size = self._calculate_window_size()
        # Get last N transcriptions (or all if fewer available)
        window_transcriptions = all_transcriptions[-window_size:] if len(all_transcriptions) > window_size else all_transcriptions

        if self.timecode_in_summary:
            return _serialize_window_with_timecodes(
                window_transcriptions,
                which=which,
                min_interval_seconds=self.timecode_min_interval_seconds,
            )

        # Legacy path: simple concatenation, no anchors.
        if which == 'translated':
            key = 'translated'
        elif which == 'text':
            key = 'text'
        else:
            raise ValueError(f"which must be 'translated' or 'text', got {which!r}")
        texts = [t.get(key, '') for t in window_transcriptions if t.get(key, '').strip()]
        return " ".join(texts)

    def _get_token_and_decrement(self):
        """Get current token, refresh client if token changed, and decrement daily capacity."""
        try:
            from trns.bot.utils import (
                decrement_daily_capacity,
                get_current_token,
                get_text,
                load_metadata,
            )

            token = get_current_token(self.api_key_file, self.metadata_path)
            if token is None:
                try:
                    metadata = load_metadata(self.metadata_path)
                    error_msg = get_text(metadata, "no_tokens_available")
                    logger.error(error_msg)
                except Exception:
                    logger.error("No tokens available")
                return None

            has_capacity = decrement_daily_capacity(self.capacity_path)
            if not has_capacity:
                logger.warning("Daily capacity exhausted; skipping LM API call.")
                return None

            # Refresh client only after capacity is reserved (token may have rotated)
            if self.client is None or self.client.api_key != token:
                from openai import OpenAI
                self.client = OpenAI(
                    base_url="https://openrouter.ai/api/v1",
                    api_key=token,
                )
                logger.info("LM client refreshed with new token")

            return token
        except ImportError:
            return None

    def _get_language_name(self, language_code: str) -> str:
        """Convert language code to full name for prompt substitution"""
        language_names = {
            'en': 'English',
            'es': 'Spanish',
            'fr': 'French',
            'de': 'German',
            'it': 'Italian',
            'pt': 'Portuguese',
            'zh': 'Chinese',
            'ja': 'Japanese',
            'ko': 'Korean',
            'ar': 'Arabic',
            'ru': 'Russian',
            'uk': 'Ukrainian',
            'pl': 'Polish',
            'tr': 'Turkish',
            'nl': 'Dutch',
            'sv': 'Swedish',
            'da': 'Danish',
            'no': 'Norwegian',
            'fi': 'Finnish',
        }
        return language_names.get(language_code, language_code.upper())

    def process_original_language(self, text: str, language_code: str) -> Optional[str]:
        """
        Process text in its original language through LM.

        Args:
            text: Original language text to process
            language_code: Language code (e.g., 'en', 'fr', 'es')

        Returns:
            Processed text in original language, or None if failed
        """
        if self.client is None or self.prompt_original_template is None:
            logger.error("LM client or original prompt template not initialized")
            return None

        if not text.strip():
            logger.debug("No text to process in original language")
            return None

        # Get language name for prompt substitution
        language_name = self._get_language_name(language_code)

        # Replace {LANGUAGE} placeholder with actual language
        prompt = self.prompt_original_template.replace('{LANGUAGE}', language_name)

        # Reserve capacity once per logical request (not per retry)
        if self._get_token_and_decrement() is None:
            return None

        max_retries = 3
        for attempt in range(max_retries):
            try:
                # Metadata prefix (title + description) goes between prompt and
                # transcript text so the model sees: instructions → context →
                # transcript. The prefix is already wrapped in <video_metadata>.
                content = prompt + "\n\n" + self.video_metadata_prefix + text

                logger.info(f"Processing original language ({language_name}) through LM...")

                # Call LM API
                messages = [
                    {
                        "role": "system",
                        "content": f"You are a text editor. Process the text in {language_name}. DO NOT translate to any other language!"
                    },
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": content
                            }
                        ]
                    }
                ]

                completion = self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                )

                # Safely extract response
                if not completion or not hasattr(completion, 'choices') or not completion.choices:
                    logger.error("LM API returned empty completion or no choices")
                    return None

                if not completion.choices[0].message:
                    logger.error("LM API returned no message in choice")
                    return None

                report_content = completion.choices[0].message.content
                if report_content is None:
                    logger.error("LM API returned None for message content")
                    return None

                report = report_content.strip('\n')

                if report:
                    logger.info(f"Original language report generated successfully ({len(report)} chars)")
                    return report
                else:
                    logger.warning("LM returned empty report for original language")
                    return None

            except Exception as e:
                error_str = str(e).lower()

                # Check for 429 error (rate limit)
                if "429" in error_str or "too many requests" in error_str:
                    logger.warning("Received 429 error (rate limit). Retrying with backoff...")
                    if attempt < max_retries - 1:
                        import time
                        time.sleep(2 ** attempt)  # Exponential backoff
                        continue
                    else:
                        logger.error("Max retries reached for 429 error")
                        raise
                else:
                    # Other error
                    logger.error(f"Error processing original language through LM: {e}")
                    import traceback
                    logger.debug(traceback.format_exc())
                    return None

        return None

    def process_russian_translation(self, text: str) -> Optional[str]:
        """
        Process Russian translation through LM.

        Args:
            text: Russian text to process

        Returns:
            Processed Russian text, or None if failed
        """
        if self.client is None or self.prompt_russian is None:
            logger.error("LM client or Russian prompt not initialized")
            return None

        if not text.strip():
            logger.debug("No Russian text to process")
            return None

        # Reserve capacity once per logical request (not per retry)
        if self._get_token_and_decrement() is None:
            return None

        max_retries = 3
        for attempt in range(max_retries):
            try:
                content = self.prompt_russian + "\n\n" + self.video_metadata_prefix + text

                logger.info("Processing Russian translation through LM...")

                # Call LM API
                messages = [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": content
                            }
                        ]
                    }
                ]

                completion = self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                )

                # Safely extract response
                if not completion or not hasattr(completion, 'choices') or not completion.choices:
                    logger.error("LM API returned empty completion or no choices")
                    return None

                if not completion.choices[0].message:
                    logger.error("LM API returned no message in choice")
                    return None

                report_content = completion.choices[0].message.content
                if report_content is None:
                    logger.error("LM API returned None for message content")
                    return None

                report = report_content.strip('\n')

                if report:
                    logger.info(f"Russian report generated successfully ({len(report)} chars)")
                    return report
                else:
                    logger.warning("LM returned empty report for Russian")
                    return None

            except Exception as e:
                error_str = str(e).lower()

                # Check for 429 error (rate limit)
                if "429" in error_str or "too many requests" in error_str:
                    logger.warning("Received 429 error (rate limit). Retrying with backoff...")
                    if attempt < max_retries - 1:
                        import time
                        time.sleep(2 ** attempt)  # Exponential backoff
                        continue
                    else:
                        logger.error("Max retries reached for 429 error")
                        raise
                else:
                    # Other error
                    logger.error(f"Error processing Russian through LM: {e}")
                    import traceback
                    logger.debug(traceback.format_exc())
                    return None

        return None

    def process_transcription_window(self, all_transcriptions: List[Dict]):
        """
        Process a window of transcriptions through the language model.
        Makes two separate API calls if bilingual mode is active:
        1. Process original language text
        2. Process Russian translation

        Args:
            all_transcriptions: List of transcription dicts with 'text' and 'translated' keys

        Returns:
            If bilingual mode and non-Russian: tuple of (original_text, russian_text)
            Otherwise: russian_text (string) or None if processing failed
        """
        if self.client is None:
            logger.error("LM client not initialized")
            return None

        # Get window text (Russian translation)
        russian_text = self._get_window_text(all_transcriptions)

        if not russian_text.strip():
            logger.debug("No text in window to process")
            return None

        logger.info(f"Processing {len(all_transcriptions)} transcriptions through LM (window: {self._calculate_window_size()})...")

        # Decide whether to make one or two API calls
        if self.use_bilingual and self.detected_language != "ru":
            # Two API calls: original language first, then Russian
            logger.info(f"Bilingual mode: processing in {self.detected_language} and Russian")

            # Get original language text from transcriptions. When timecodes
            # are enabled we route through _get_window_text(which='text') so
            # the original window gets the same sparse markers as Russian.
            # Otherwise preserve the legacy newline-joined shape.
            if self.timecode_in_summary:
                original_text = self._get_window_text(all_transcriptions, which='text')
            else:
                original_text = "\n".join([t.get('text', '') for t in all_transcriptions[-self._calculate_window_size():]])

            # Process original language
            original_output = self.process_original_language(original_text, self.detected_language)

            # Process Russian translation
            russian_output = self.process_russian_translation(russian_text)

            if original_output and russian_output:
                logger.info(f"Successfully processed both versions - original: {len(original_output)} chars, russian: {len(russian_output)} chars")
                return (original_output, russian_output)
            elif russian_output:
                # If original failed but Russian succeeded, return Russian only
                logger.warning("Original language processing failed, returning Russian only")
                return russian_output
            elif original_output:
                # If Russian failed but original succeeded, still return the
                # usable report instead of dropping the whole LM response.
                logger.warning("Russian processing failed, returning original language only")
                return original_output
            else:
                logger.error("Both original and Russian processing failed")
                return None
        else:
            # One API call: Russian only
            logger.info(f"Russian-only mode (use_bilingual={self.use_bilingual}, detected_language={self.detected_language})")
            return self.process_russian_translation(russian_text)

    def worker(
        self,
        lm_queue,
        lm_result_queue,
        all_transcriptions_getter: Callable[[], List[Dict]]
    ):
        """
        Background worker that processes transcriptions through LM.

        Args:
            lm_queue: Queue receiving (translated_text, timestamp, metadata) tuples
            lm_result_queue: Queue to put (report_text, timestamp) results
            all_transcriptions_getter: Callable that returns current list of all transcriptions
        """
        logger.info("LM worker thread started")

        while not self._check_shutdown():
            try:
                # Get item from queue (with timeout to check shutdown_flag)
                try:
                    item = lm_queue.get(timeout=1.0)
                except queue.Empty:
                    # Timeout - check shutdown_flag and continue
                    continue

                if item is None:  # Shutdown signal
                    logger.debug("LM worker received shutdown signal")
                    break

                # Drain all other queued items (skip stale windows)
                # We only want to process the latest window, not every historical shift
                drained_count = 0
                shutdown_requested = False
                while True:
                    try:
                        old_item = lm_queue.get_nowait()
                        if old_item is None:  # Shutdown signal
                            lm_queue.task_done()
                            shutdown_requested = True
                            break
                        lm_queue.task_done()
                        drained_count += 1
                    except queue.Empty:
                        break

                if shutdown_requested:
                    logger.debug("LM worker received shutdown signal while draining queue")
                    break

                if drained_count > 0:
                    logger.debug(f"Drained {drained_count} stale queued items, processing latest window")

                # Mark the current item as done
                lm_queue.task_done()

                # Get current state of all transcriptions (always use latest)
                all_transcriptions = all_transcriptions_getter()

                if not all_transcriptions:
                    logger.debug("No transcriptions available for LM processing")
                    continue

                # Process latest window through LM
                report = self.process_transcription_window(all_transcriptions)

                if report:
                    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    lm_result_queue.put({
                        'report': report,
                        'timestamp': timestamp,
                        'num_transcriptions': len(all_transcriptions)
                    })

            except Exception as e:
                logger.error(f"Error in LM worker: {e}")
                import traceback
                logger.debug(traceback.format_exc())
                # Mark current item as done if we got one
                if 'item' in locals() and item is not None:
                    try:
                        lm_queue.task_done()
                    except ValueError:
                        pass

        # Clean up any remaining items in queue on shutdown
        logger.debug("LM worker shutting down...")
        while not lm_queue.empty():
            try:
                item = lm_queue.get_nowait()
                if item is not None:
                    lm_queue.task_done()
            except queue.Empty:
                break

        logger.info("LM worker thread stopped")
