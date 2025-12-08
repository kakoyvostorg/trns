"""
Transcription Pipeline Module

Main orchestration logic for the transcription pipeline.
Manages three parallel processes: audio extraction, transcription, and LM processing.
"""

import time
import sys
import threading
import queue
import re
import os
from datetime import datetime
from typing import Optional, List, Dict, Callable
import logging

from .subtitle_extractor import YouTubeSubtitleExtractor
from .whisper_transcriber import WhisperTranscriber
from .language_model import LMProcessor

logger = logging.getLogger(__name__)


def extract_video_id(url_or_id: str) -> str:
    """Extract video ID from YouTube URL"""
    if "youtube.com" in url_or_id or "youtu.be" in url_or_id:
        if "v=" in url_or_id:
            return url_or_id.split("v=")[-1].split("&")[0]
        elif "youtu.be/" in url_or_id:
            return url_or_id.split("youtu.be/")[-1].split("?")[0]
    return url_or_id


class TranscriptionPipeline:
    """
    Main transcription pipeline that orchestrates audio extraction,
    transcription, translation, and LM processing.
    """
    
    def __init__(
        self,
        video_id: str,
        args,
        shutdown_flag: Callable[[], bool]
    ):
        """
        Initialize transcription pipeline.
        
        Args:
            video_id: YouTube video ID
            args: Parsed command-line arguments
            shutdown_flag: Callable that returns True if shutdown requested
        """
        self.video_id = video_id
        self.args = args
        self.shutdown_flag = shutdown_flag
        
        # Initialize components
        self.subtitle_extractor = None
        self.whisper_transcriber = None
        self.lm_processor = None
        
        # State tracking
        self.all_transcribed_text = []  # List of transcription dicts
        self.iteration = 0
        self.current_time_position = 0.0
        self.timing_history = []
        self.max_history = 5
        self.subtitles_fully_processed = False  # Track if all subtitles have been processed
        
        # Buffers
        self.text_buffer = ""
        self.translated_buffer = ""
        
        # Overlap tracking
        self.overlap_seconds = args.overlap if hasattr(args, 'overlap') else 2
        self.last_chunk_end_text = ""
        self.last_chunk_end_translated = ""
        
        # Duplicate detection for breaking on repeated transcriptions
        self.last_transcription_text = None
        
        # Queues
        self.transcription_queue = None
        self.result_queue = None
        self.lm_queue = None
        self.lm_result_queue = None
        
        # Threads
        self.transcription_thread = None
        self.lm_thread = None
        
        # Video info
        self.is_live_video = False
        self.video_duration = None
        self.process_mode = None
        
        # Flags
        self.use_subtitles = False
        self.use_whisper = False
        self.debug_mode = False  # Will be set from main.py
        
        # LM interval tracking (controlled by pipeline, not worker thread)
        self.lm_interval = getattr(args, 'lm_interval', 30)
        self.last_lm_call_time = 0.0
        
        # User settings for output control
        self.show_original_translation = getattr(args, 'show_original_translation', True)
        self.show_transcription = getattr(args, 'show_transcription', True)
        
        # Track detected language for bilingual LM mode
        self.detected_language = "ru"  # Default to Russian
    
    def initialize_components(self):
        """Initialize subtitle extractor, whisper transcriber, and LM processor"""
        # Initialize subtitle extractor if needed
        if self.args.method in ["auto", "subtitles"]:
            self.subtitle_extractor = YouTubeSubtitleExtractor(self.video_id)
            logger.info("Checking for available subtitles...")
            available, languages = self.subtitle_extractor.check_subtitles_available()
            
            if available:
                logger.info(f"✓ Subtitles available in: {', '.join(languages)}")
                if self.args.method == "subtitles":
                    logger.info("Using subtitle extraction method")
                    self.use_subtitles = True
                else:
                    self.use_subtitles = True
            else:
                logger.warning("No subtitles available for this video")
                if self.args.method == "subtitles":
                    logger.error("Subtitles requested but not available.")
                    raise RuntimeError("Subtitles requested but not available for this video")
                elif self.args.method == "auto":
                    logger.info("Falling back to Whisper transcription")
                    self.use_subtitles = False
                    self.use_whisper = True
        else:
            self.use_subtitles = False
        
        # Initialize whisper transcriber if needed
        if self.args.method in ["auto", "whisper"]:
            if not self.use_subtitles or self.args.method == "whisper":
                try:
                    self.whisper_transcriber = WhisperTranscriber(
                        model_size=self.args.whisper_model,
                        use_faster_whisper=self.args.use_faster_whisper,
                        shutdown_flag=self.shutdown_flag
                    )
                    logger.info("Whisper transcriber initialized")
                    self.use_whisper = True
                except Exception as e:
                    logger.error(f"Failed to initialize Whisper: {e}")
                    if self.args.method == "whisper":
                        raise RuntimeError(f"Failed to initialize Whisper: {e}")
                    elif self.args.method == "auto":
                        logger.warning("Falling back to subtitle extraction only")
                        self.use_whisper = False
        
        # Get video info
        if self.whisper_transcriber:
            info = self.whisper_transcriber._get_video_info(self.video_id)
            if info:
                self.is_live_video = info.get('is_live', False)
                self.video_duration = info.get('duration')
                logger.info(f"Video is {'live' if self.is_live_video else 'non-live'}")
                if self.video_duration:
                    logger.info(f"Video duration: {self.video_duration:.1f} seconds ({self.video_duration/60:.1f} minutes)")
        
        # Determine processing mode
        if self.args.process_mode == "auto":
            self.process_mode = "chunked" if self.is_live_video else "full"
        else:
            self.process_mode = self.args.process_mode
        
        logger.info(f"Processing mode: {self.process_mode}")
        
        # Initialize LM processor if LM output mode is enabled
        if hasattr(self.args, 'lm_output_mode') and self.args.lm_output_mode != "transcriptions-only":
            try:
                # Determine if we should use bilingual mode
                # Use bilingual if: show_original_translation is ON AND we expect non-Russian content
                use_bilingual_mode = self.show_original_translation
                
                self.lm_processor = LMProcessor(
                    api_key_file=getattr(self.args, 'lm_api_key_file', 'api_key.txt'),
                    prompt_file=getattr(self.args, 'lm_prompt_file', 'prompt.md'),
                    prompt_original_file=getattr(self.args, 'lm_prompt_original_file', 'prompt_original.md'),
                    model=getattr(self.args, 'lm_model', 'google/gemma-3-27b-it:free'),
                    window_seconds=getattr(self.args, 'lm_window_seconds', 120),
                    interval=self.args.interval,
                    context=getattr(self.args, 'context', ''),
                    shutdown_flag=self.shutdown_flag,
                    use_bilingual=use_bilingual_mode,
                    detected_language=self.detected_language
                )
                logger.info(f"LM processor initialized (bilingual mode: {use_bilingual_mode})")
            except Exception as e:
                logger.error(f"Failed to initialize LM processor: {e}")
                logger.warning("Continuing without LM processing")
                self.lm_processor = None
    
    def _setup_parallel_processing(self):
        """Setup queues and worker threads for parallel processing"""
        if self.use_whisper and self.whisper_transcriber:
            # Transcription queues
            self.transcription_queue = queue.Queue()
            self.result_queue = queue.Queue()
            
            # Start transcription worker
            self.transcription_thread = threading.Thread(
                target=self._transcription_worker,
                daemon=True
            )
            self.transcription_thread.start()
            logger.info("Parallel transcription pipeline initialized")
        
        # LM queues and worker
        if self.lm_processor:
            self.lm_queue = queue.Queue()
            self.lm_result_queue = queue.Queue()
            
            # Start LM worker
            self.lm_thread = threading.Thread(
                target=self.lm_processor.worker,
                args=(self.lm_queue, self.lm_result_queue, lambda: self.all_transcribed_text),
                daemon=True
            )
            self.lm_thread.start()
            logger.info("LM processing pipeline initialized")
    
    def _transcription_worker(self):
        """Background worker that transcribes audio chunks from the queue"""
        while not self.shutdown_flag():
            try:
                # Get chunk from queue (with timeout to check shutdown_flag)
                try:
                    item = self.transcription_queue.get(timeout=1.0)
                except queue.Empty:
                    # Timeout - check shutdown_flag and continue
                    continue
                
                if item is None:  # Shutdown signal
                    logger.debug("Transcription worker received shutdown signal")
                    break
                
                audio_path, chunk_iteration, chunk_timestamp, extract_time, overlap_text, overlap_translated = item
                
                if not audio_path:
                    # Audio extraction failed - log and skip
                    error_msg = f"\n[ERROR] Audio extraction failed for chunk #{chunk_iteration}. Skipping transcription.\n"
                    logger.error(f"Audio path is None for chunk #{chunk_iteration}, skipping transcription")
                    print(error_msg, flush=True)  # Send error to Telegram via stdout capture
                    self.transcription_queue.task_done()
                    continue
                
                # Audio path is valid, proceed with transcription
                transcribe_start = time.time()
                logger.info(f"Transcribing chunk #{chunk_iteration}...")
                text, detected_language, language_prob = self.whisper_transcriber.transcribe_audio(audio_path)
                transcribe_time = time.time() - transcribe_start
                    
                # Translate if needed
                if text.strip():
                    if detected_language != 'ru':
                        translate_start = time.time()
                        translated_text = self.whisper_transcriber.translate_to_russian(text, detected_language)
                        translate_time = time.time() - translate_start
                    else:
                        translated_text = text
                        translate_time = 0.0
                    
                    # Remove overlap from beginning of current chunk if we have overlap text
                    if overlap_text and overlap_text.strip():
                        # Find overlap text at the start of current chunk
                        text_lower = text.lower().strip()
                        overlap_lower = overlap_text.lower().strip()
                        
                        # Try to find and remove overlap (check first N words)
                        if text_lower.startswith(overlap_lower):
                            # Exact match at start
                            text = text[len(overlap_text):].strip()
                            translated_text = translated_text[len(overlap_translated):].strip() if len(translated_text) >= len(overlap_translated) else translated_text
                            logger.debug(f"Removed exact overlap from chunk #{chunk_iteration}")
                        else:
                            # Try to find overlap by matching first few words
                            text_words = text.split()
                            overlap_words = overlap_text.split()
                            if len(overlap_words) > 0 and len(text_words) > 0:
                                # Check if first N words match (where N = overlap word count)
                                match_count = 0
                                for i in range(min(len(overlap_words), len(text_words))):
                                    if text_words[i].lower() == overlap_words[i].lower():
                                        match_count += 1
                                    else:
                                        break
                                
                                # If significant overlap found (at least 3 words or 50% of overlap), remove it
                                if match_count >= min(3, len(overlap_words) * 0.5):
                                    text = " ".join(text_words[match_count:]).strip()
                                    # For translated, try similar approach
                                    trans_words = translated_text.split()
                                    trans_overlap_words = overlap_translated.split()
                                    if len(trans_words) > match_count:
                                        translated_text = " ".join(trans_words[match_count:]).strip()
                                    logger.debug(f"Removed {match_count} overlapping words from chunk #{chunk_iteration}")
                    
                    # Store last N words for next chunk overlap detection
                    words_to_store = max(4, int(self.overlap_seconds * 2))
                    text_words = text.split()
                    trans_words = translated_text.split()
                    
                    if len(text_words) >= words_to_store:
                        self.last_chunk_end_text = " ".join(text_words[-words_to_store:])
                        self.last_chunk_end_translated = " ".join(trans_words[-words_to_store:]) if len(trans_words) >= words_to_store else translated_text
                    else:
                        self.last_chunk_end_text = text
                        self.last_chunk_end_translated = translated_text
                    
                    # Put result in queue
                    self.result_queue.put({
                        'iteration': chunk_iteration,
                        'timestamp': chunk_timestamp,
                        'text': text,
                        'translated_text': translated_text,
                        'detected_language': detected_language,
                        'language_prob': language_prob,
                        'extract_time': extract_time,
                        'transcribe_time': transcribe_time,
                        'translate_time': translate_time
                    })
                    
                    # Send to LM queue if LM is enabled and interval has elapsed
                    if self.lm_processor and self.lm_queue:
                        current_time = time.time()
                        if current_time - self.last_lm_call_time >= self.lm_interval:
                            self.lm_queue.put((translated_text, chunk_timestamp, {
                                'iteration': chunk_iteration,
                                'language': detected_language
                            }))
                            self.last_lm_call_time = current_time
                            if self.debug_mode:
                                logger.debug(f"Added transcription to LM queue (interval: {self.lm_interval}s)")
                        else:
                            time_since_last = current_time - self.last_lm_call_time
                            if self.debug_mode:
                                logger.debug(f"Skipping LM queue (interval not elapsed: {time_since_last:.1f}s < {self.lm_interval}s)")
                else:
                    logger.debug(f"No speech detected in chunk #{chunk_iteration}")
                    # Reset overlap tracking if no text
                    self.last_chunk_end_text = ""
                    self.last_chunk_end_translated = ""
                
                self.transcription_queue.task_done()
            except Exception as e:
                logger.error(f"Error in transcription worker: {e}")
                if 'item' in locals() and item is not None:
                    self.transcription_queue.task_done()
        
        # Clean up any remaining items in queue on shutdown
        logger.debug("Transcription worker shutting down...")
        while not self.transcription_queue.empty():
            try:
                item = self.transcription_queue.get_nowait()
                if item is not None:
                    self.transcription_queue.task_done()
            except queue.Empty:
                break
    
    def _process_full_video(self):
        """Process entire video at once (for non-live videos)"""
        if not (self.process_mode == "full" and not self.is_live_video and self.use_whisper and self.whisper_transcriber):
            return False
        
        logger.info("Processing entire video with progress bar...")
        try:
            from tqdm import tqdm
            
            # Download entire audio file
            if self.shutdown_flag():
                logger.info("Shutdown requested, exiting...")
                return True
            
            logger.info("Downloading entire audio file...")
            audio_path = self.whisper_transcriber.extract_audio_from_youtube(self.video_id, duration=1, start_time=None)
            
            if self.shutdown_flag():
                logger.info("Shutdown requested, exiting...")
                return True
            
            if not audio_path:
                logger.error("Failed to download audio file")
                raise RuntimeError("Failed to download audio file from video")
            
            # Transcribe with progress bar
            if self.shutdown_flag():
                logger.info("Shutdown requested, exiting...")
                return True
            
            logger.info("Detecting language...")
            detected_language, lang_prob = self.whisper_transcriber._detect_language(audio_path)
            logger.info(f"Detected language: {detected_language} (probability: {lang_prob:.2f})")
            
            if self.shutdown_flag():
                logger.info("Shutdown requested, exiting...")
                return True
            
            # Use Whisper with appropriate model based on language
            model = self.whisper_transcriber._get_transcription_model(detected_language)
            model_size = "tiny" if detected_language == "en" else "small"
            logger.info(f"Using {model_size} model for {detected_language} transcription")
            
            if self.whisper_transcriber.use_faster_whisper:
                segments, info = model.transcribe(audio_path, beam_size=5)
                
                # Collect segments with progress bar
                all_segments = []
                pbar = tqdm(desc="Transcribing (Whisper)", unit="segment", dynamic_ncols=True)
                try:
                    for segment in segments:
                        # Check shutdown flag during transcription
                        if self.shutdown_flag():
                            logger.info("Shutdown requested, stopping transcription...")
                            break
                        all_segments.append(segment)
                        pbar.update(1)
                        pbar.set_postfix({"text": segment.text[:50] + "..." if len(segment.text) > 50 else segment.text})
                finally:
                    pbar.close()
                
                text = " ".join([seg.text for seg in all_segments]).strip()
                if detected_language != "unknown":
                    final_language = detected_language
                    final_prob = lang_prob
                else:
                    final_language = info.language if hasattr(info, 'language') else "unknown"
                    final_prob = info.language_probability if hasattr(info, 'language_probability') else 0.0
                detected_language = final_language
                language_prob = final_prob
            else:
                # For openai-whisper, show progress during transcription
                with tqdm(desc="Transcribing (Whisper)", total=100) as pbar:
                        # Note: openai-whisper doesn't support interruption, but we check before/after
                        if self.shutdown_flag():
                            logger.info("Shutdown requested, skipping transcription...")
                            text = ""
                            detected_language = "unknown"
                            language_prob = 0.0
                        else:
                            result = model.transcribe(audio_path, verbose=False)
                            pbar.update(100)
                            if self.shutdown_flag():
                                logger.info("Shutdown requested after transcription...")
                            
                            text = result["text"].strip()
                            if detected_language != "unknown":
                                final_language = detected_language
                                final_prob = lang_prob
                            else:
                                final_language = result.get("language", "unknown")
                                final_prob = 1.0
                            detected_language = final_language
                            language_prob = final_prob
            
            # Translate in chunks (check shutdown before translation)
            if self.shutdown_flag():
                logger.info("Shutdown requested, skipping translation and output...")
                translated_text = text if detected_language == 'ru' else ""
            elif text.strip():
                logger.info("Translating text...")
                if detected_language != 'ru':
                    translated_text = self.whisper_transcriber.translate_to_russian(text, detected_language)
                else:
                    translated_text = text
                
                # Output results
                timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                self._output_transcription(timestamp, text, translated_text, detected_language, language_prob)
                
                # Store transcription for LM processing
                self.all_transcribed_text.append({
                    'timestamp': timestamp,
                    'text': text,
                    'translated': translated_text,
                    'iteration': 1,
                    'language': detected_language,
                    'language_prob': language_prob
                })
                
                # Process through LM if enabled
                if self.lm_processor and text.strip():
                    logger.info("Processing transcription through LM...")
                    report = self.lm_processor.process_transcription_window(self.all_transcribed_text)
                    if report:
                        report_timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        self._output_lm_report(report_timestamp, report)
            
            # Clean up audio file
            try:
                if os.path.exists(audio_path):
                    os.remove(audio_path)
                    logger.debug("Cleaned up audio file")
            except Exception as e:
                logger.debug(f"Error cleaning up audio file: {e}")
            
            logger.info("Transcription complete!")
            return True
            
        except ImportError:
            logger.error("tqdm not installed. Install with: pip install tqdm")
            logger.info("Falling back to chunked processing...")
            self.process_mode = "chunked"
            return False
        except Exception as e:
            logger.error(f"Error in full video processing: {e}")
            logger.info("Falling back to chunked processing...")
            self.process_mode = "chunked"
            return False
    
    def _output_transcription(self, timestamp: str, text: str, translated_text: str, language: str, language_prob: float, iteration: Optional[int] = None):
        """Output transcription based on output mode and user settings"""
        # Update detected language for LM processor
        if language and language != "unknown":
            self.detected_language = language
            # Update LM processor's detected language if it exists
            if self.lm_processor:
                self.lm_processor.detected_language = language
                # Reload prompt if language changed and bilingual mode is on
                if self.lm_processor.use_bilingual:
                    try:
                        self.lm_processor._load_prompt()
                    except Exception as e:
                        logger.warning(f"Failed to reload prompt after language detection: {e}")
        
        lm_output_mode = getattr(self.args, 'lm_output_mode', 'both')
        
        # Check if we should output transcription
        if not self.show_transcription:
            # Transcription output is disabled by user
            logger.debug(f"Transcription output suppressed by user setting")
            # Still save to file if requested
            if self.args.save_transcript:
                try:
                    with open(self.args.save_transcript, 'a', encoding='utf-8') as f:
                        f.write(f"[{timestamp}] {text}\n")
                        if self.args.translation_output in ["russian-only", "both"]:
                            f.write(f"[{timestamp}] [RU] {translated_text}\n")
                except Exception as e:
                    logger.warning(f"Failed to save transcript: {e}")
            return
        
        # Determine what to output
        if lm_output_mode == "transcriptions-only" or lm_output_mode == "both":
            # Output transcription based on user setting and language
            # If show_original_translation is ON and language is not Russian: show both in separate messages
            # Otherwise: show Russian only
            iter_str = f" #{iteration}" if iteration else ""
            # In production mode, only log to file; in debug mode, log to stdout
            if self.debug_mode:
                logger.info(f"🎤 Chunk{iter_str} ({language}, prob={language_prob:.2f}): {text[:50]}...")
            else:
                logger.debug(f"🎤 Chunk{iter_str} ({language}, prob={language_prob:.2f}): {text[:50]}...")
            
            # Always print transcription to stdout (this is the main output)
            if self.show_original_translation and language != "ru" and language != "unknown":
                # Print original with language label
                print(f"\n[{language}] {text}\n", flush=True)
                # Print Russian translation without label (add hidden marker for immediate send)
                print(f"\n{translated_text} [.]\n", flush=True)
            else:
                # Print Russian only
                print(f"\n{translated_text}\n", flush=True)
            
            # Save to file if requested
            if self.args.save_transcript:
                try:
                    with open(self.args.save_transcript, 'a', encoding='utf-8') as f:
                        f.write(f"[{timestamp}] {text}\n")
                        if self.args.translation_output in ["russian-only", "both"]:
                            f.write(f"[{timestamp}] [RU] {translated_text}\n")
                except Exception as e:
                    logger.warning(f"Failed to save transcript: {e}")
    
    def _output_lm_report(self, timestamp: str, report):
        """
        Output LM report (handles both string and tuple formats).
        
        Args:
            timestamp: Timestamp string
            report: Either a string (Russian-only) or tuple (original, russian) for bilingual mode
        """
        lm_output_mode = getattr(self.args, 'lm_output_mode', 'both')
        
        if lm_output_mode == "lm-only" or lm_output_mode == "both":
            # Check if report is bilingual (tuple)
            if isinstance(report, tuple) and len(report) == 2:
                original_text, russian_text = report
                
                # In production mode, only log to file; in debug mode, log to stdout
                if self.debug_mode:
                    logger.info(f"📊 Bilingual LM Report: original={original_text[:50]}..., russian={russian_text[:50]}...")
                else:
                    logger.debug(f"📊 Bilingual LM Report generated")
                
                # Print LM Report label, then original, then Russian (3 separate messages)
                # Add hidden markers to trigger immediate sends in Telegram bot
                print(f"\n[LM Report]\n", flush=True)
                print(f"\n{original_text} [.]\n", flush=True)
                print(f"\n{russian_text} [.]\n", flush=True)
                
                # Save to file if requested
                if self.args.save_transcript:
                    try:
                        with open(self.args.save_transcript, 'a', encoding='utf-8') as f:
                            f.write(f"[{timestamp}] [LM Report - Original]\n{original_text}\n")
                            f.write(f"[{timestamp}] [LM Report - Russian]\n{russian_text}\n")
                    except Exception as e:
                        logger.warning(f"Failed to save LM report: {e}")
            else:
                # Russian-only mode (string)
                report_str = str(report)
                
                # In production mode, only log to file; in debug mode, log to stdout
                if self.debug_mode:
                    logger.info(f"📊 LM Report: {report_str[:50]}...")
                else:
                    logger.debug(f"📊 LM Report: {report_str[:50]}...")
                
                # Always print LM report to stdout (this is the main output)
                # Print LM Report label as separate message, then the report
                print(f"\nLM Report:\n{report_str}\n", flush=True)
                
                # Save to file if requested
                if self.args.save_transcript:
                    try:
                        with open(self.args.save_transcript, 'a', encoding='utf-8') as f:
                            f.write(f"[{timestamp}] [LM Report]\n{report_str}\n")
                    except Exception as e:
                        logger.warning(f"Failed to save LM report: {e}")
    
    def _process_transcription_results(self):
        """Process completed transcription results from queue"""
        try:
            while True:
                result = self.result_queue.get_nowait()
                result_timestamp = result['timestamp']
                result_text = result['text']
                result_translated = result['translated_text']
                result_lang = result['detected_language']
                result_lang_prob = result['language_prob']
                
                # Store in cumulative list
                self.all_transcribed_text.append({
                    'timestamp': result_timestamp,
                    'text': result_text,
                    'translated': result_translated,
                    'iteration': result['iteration'],
                    'language': result_lang,
                    'language_prob': result_lang_prob
                })
                
                # Check if transcription is duplicate (same as last one) - break if so
                if self.last_transcription_text is not None and result_text.strip() == self.last_transcription_text.strip():
                    logger.info(f"Duplicate transcription detected (same as last): '{result_text[:50]}...'. Breaking loop.")
                    return True  # Signal to break
                self.last_transcription_text = result_text
                
                # Add to sentence buffers
                self.text_buffer += " " + result_text if self.text_buffer else result_text
                self.translated_buffer += " " + result_translated if self.translated_buffer else result_translated
                
                # Extract complete sentences (ending with . ! or ?)
                sentence_pattern = r'[^.!?]*[.!?]\s*'
                
                # Find all complete sentences
                complete_sentences = re.findall(sentence_pattern, self.text_buffer)
                complete_translated_sentences = re.findall(sentence_pattern, self.translated_buffer)
                
                # Output complete sentences
                if complete_sentences:
                    # Join complete sentences
                    output_text = "".join(complete_sentences).strip()
                    output_translated = "".join(complete_translated_sentences).strip()
                    
                    # Remove complete sentences from buffer
                    complete_length = sum(len(s) for s in complete_sentences)
                    self.text_buffer = self.text_buffer[complete_length:].strip()
                    
                    complete_translated_length = sum(len(s) for s in complete_translated_sentences)
                    self.translated_buffer = self.translated_buffer[complete_translated_length:].strip()
                    
                    # Output transcription
                    self._output_transcription(
                        result_timestamp,
                        output_text,
                        output_translated,
                        result_lang,
                        result_lang_prob,
                        iteration=result['iteration']
                    )
                
                # Update timing metrics
                total_time = result['extract_time'] + result['transcribe_time'] + result['translate_time']
                self.timing_history.append((total_time, self.args.interval))
                if len(self.timing_history) > self.max_history:
                    self.timing_history.pop(0)
                
        except queue.Empty:
            pass  # No results ready yet
        
        return False  # No duplicate found, continue processing
    
    def _process_lm_results(self):
        """Process completed LM reports from queue"""
        try:
            while True:
                result = self.lm_result_queue.get_nowait()
                self._output_lm_report(result['timestamp'], result['report'])
        except queue.Empty:
            pass  # No results ready yet
    
    def run(self):
        """Run the main transcription pipeline"""
        # Initialize components
        self.initialize_components()
        
        # Print context at the beginning in production mode
        context = getattr(self.args, 'context', '')
        if not self.debug_mode and context and context.strip():
            print(f"\nДополнительный контекст: {context}\n", flush=True)
        
        # Try full video processing first
        if self._process_full_video():
            return
        
        # Setup parallel processing
        self._setup_parallel_processing()
        
        # Main transcription loop (for live streams or chunked processing)
        logger.info(f"Starting {'real-time' if self.is_live_video else 'chunked'} transcription (interval: {self.args.interval}s)")
        logger.info("Press Ctrl+C to stop")
        logger.info("-" * 80)
        
        next_audio_path = None
        next_extract_time = 0
        first_chunk = True
        consecutive_failures = 0
        MAX_CONSECUTIVE_FAILURES = 5  # Stop after 5 consecutive failures
        
        try:
            while not self.shutdown_flag():
                # Check shutdown_flag at the start of each iteration
                if self.shutdown_flag():
                    break
                
                # For non-live videos in chunked mode, if all subtitles have been processed, break
                if not self.is_live_video and self.process_mode == "chunked" and self.subtitles_fully_processed:
                    logger.info("All subtitle segments have been processed. Breaking loop.")
                    # Process any remaining LM results before breaking
                    if self.lm_processor:
                        self._process_lm_results()
                    break
                
                self.iteration += 1
                timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                cycle_start_time = time.time()
                
                if self.debug_mode:
                    logger.info(f"\n[{timestamp}] Check #{self.iteration}")
                else:
                    logger.debug(f"\n[{timestamp}] Check #{self.iteration}")
                
                # Try subtitles first if available
                segments = []
                subtitle_success = False
                
                if self.use_subtitles and self.subtitle_extractor:
                    try:
                        subtitle_start = time.time()
                        segments = self.subtitle_extractor.get_new_subtitles(language=self.args.language)
                        subtitle_time = time.time() - subtitle_start
                        
                        # For non-live videos in chunked mode, if we've already processed all segments
                        # and get_new_subtitles returns empty, we should break immediately
                        if not segments and not self.is_live_video and self.process_mode == "chunked":
                            if self.subtitles_fully_processed or self.subtitle_extractor.last_timestamp > 0:
                                logger.info(f"No new subtitle segments. All segments processed (last_timestamp: {self.subtitle_extractor.last_timestamp:.1f}s). Breaking immediately.")
                                self.subtitles_fully_processed = True
                                # Process any remaining LM results before breaking
                                if self.lm_processor:
                                    self._process_lm_results()
                                # Break immediately - don't continue to next iteration
                                break
                        
                        if segments:
                            # Reset failure counter on successful subtitle extraction
                            consecutive_failures = 0
                            
                            # Combine all new segments
                            text = " ".join([seg['text'] for seg in segments])
                            if text.strip():
                                logger.info(f"📝 Subtitles ({len(segments)} segments): {text[:100]}..." if len(text) > 100 else f"📝 Subtitles ({len(segments)} segments): {text}")
                                
                                # Translate subtitles if needed (for LM processing)
                                translated_text = text
                                if self.args.language != 'ru' and self.whisper_transcriber:
                                    # Try to translate subtitles for LM processing
                                    try:
                                        translated_text = self.whisper_transcriber.translate_to_russian(text, self.args.language)
                                    except:
                                        translated_text = text  # Fallback to original if translation fails
                                
                                # Store in cumulative list
                                self.all_transcribed_text.append({
                                    'timestamp': timestamp,
                                    'text': text,
                                    'translated': translated_text,
                                    'iteration': self.iteration,
                                    'language': self.args.language,
                                    'language_prob': 1.0
                                })
                                
                                # Check if transcription is duplicate (same as last one) - break if so
                                if self.last_transcription_text is not None and text.strip() == self.last_transcription_text.strip():
                                    logger.info(f"Duplicate transcription detected (same as last): '{text[:50]}...'. Breaking loop.")
                                    self.subtitles_fully_processed = True
                                    # Process any remaining LM results before breaking
                                    if self.lm_processor:
                                        self._process_lm_results()
                                    break
                                self.last_transcription_text = text
                                
                                # Output transcription
                                lm_output_mode = getattr(self.args, 'lm_output_mode', 'both')
                                if lm_output_mode == "transcriptions-only" or lm_output_mode == "both":
                                    # Use user setting for showing original
                                    if self.show_original_translation and self.args.language != "ru":
                                        # Print original with language label
                                        print(f"\n[{self.args.language}] {text}\n", flush=True)
                                        # Print Russian translation without label (add hidden marker for immediate send)
                                        print(f"\n{translated_text} [.]\n", flush=True)
                                    else:
                                        # Print Russian only
                                        print(f"\n{translated_text}\n", flush=True)
                                
                                # Save to file if requested
                                if self.args.save_transcript:
                                    try:
                                        with open(self.args.save_transcript, 'a', encoding='utf-8') as f:
                                            f.write(f"[{timestamp}] {text}\n")
                                            if self.args.translation_output in ["russian-only", "both"]:
                                                f.write(f"[{timestamp}] [RU] {translated_text}\n")
                                    except Exception as e:
                                        logger.warning(f"Failed to save transcript: {e}")
                                
                                # Send to LM queue if enabled and interval has elapsed
                                if self.lm_processor and self.lm_queue:
                                    current_time = time.time()
                                    if current_time - self.last_lm_call_time >= self.lm_interval:
                                        self.lm_queue.put((translated_text, timestamp, {
                                            'iteration': self.iteration,
                                            'language': self.args.language
                                        }))
                                        self.last_lm_call_time = current_time
                                        if self.debug_mode:
                                            logger.debug(f"Added subtitle to LM queue (interval: {self.lm_interval}s)")
                                    else:
                                        time_since_last = current_time - self.last_lm_call_time
                                        if self.debug_mode:
                                            logger.debug(f"Skipping LM queue (interval not elapsed: {time_since_last:.1f}s < {self.lm_interval}s)")
                                
                                subtitle_success = True
                                
                                # For non-live videos in chunked mode, check if we've processed all segments
                                if not self.is_live_video and self.process_mode == "chunked" and segments:
                                    # Update current_time_position based on the last segment processed
                                    last_segment = segments[-1]
                                    segment_end_time = last_segment['start'] + last_segment['duration']
                                    # The subtitle extractor's last_timestamp is already updated by get_new_subtitles()
                                    # For non-live videos, get_new_subtitles() returns ALL segments on first call
                                    # So after processing them, we should check if we're done
                                    self.current_time_position = segment_end_time
                                    
                                    # Check if we've reached the end of the video
                                    # For non-live videos, get_new_subtitles() returns ALL segments on first call
                                    # (when last_timestamp was 0.0). After that, it returns empty.
                                    # So if we got segments and last_timestamp is now set (was 0 before), we got all segments
                                    # Also check if last_timestamp is near video end, or if we got a large batch
                                    should_break = False
                                    
                                    # Check if we got all segments (large batch for non-live video)
                                    # For non-live videos, if we get many segments at once, it's likely all of them
                                    if len(segments) > 50:
                                        should_break = True
                                        logger.info(f"Detected large batch of {len(segments)} segments - assuming all segments for non-live video")
                                    
                                    # Check if we've reached the end based on video duration
                                    if self.video_duration:
                                        # If segment end is close to video end, or if last_timestamp is at/near video end
                                        if (segment_end_time >= self.video_duration * 0.95 or 
                                            self.subtitle_extractor.last_timestamp >= self.video_duration * 0.95):
                                            should_break = True
                                            logger.info(f"Reached end of video (segment_end: {segment_end_time:.1f}s, last_timestamp: {self.subtitle_extractor.last_timestamp:.1f}s, video_duration: {self.video_duration:.1f}s)")
                                    
                                    # For non-live videos, if last_timestamp is significantly greater than 0,
                                    # and we got segments, it means we processed all segments in one go
                                    # (since get_new_subtitles returns all segments on first call for non-live videos)
                                    if self.subtitle_extractor.last_timestamp > 0 and not should_break:
                                        # If we have video duration and last_timestamp is close to it, we're done
                                        if self.video_duration and self.subtitle_extractor.last_timestamp >= self.video_duration * 0.90:
                                            should_break = True
                                        # If we don't have video duration but got a substantial number of segments,
                                        # assume we got all segments (this is the first and only call for non-live videos)
                                        elif len(segments) > 20:
                                            should_break = True
                                            logger.info(f"Detected substantial batch ({len(segments)} segments) with last_timestamp {self.subtitle_extractor.last_timestamp:.1f}s - assuming all segments processed")
                                    
                                    if should_break:
                                        self.current_time_position = self.video_duration if self.video_duration else segment_end_time
                                        logger.info(f"Processed all subtitle segments (reached end at {segment_end_time:.1f}s, last_timestamp: {self.subtitle_extractor.last_timestamp:.1f}s, {len(segments)} segments). Ending processing.")
                                        # Mark that all subtitles have been processed
                                        self.subtitles_fully_processed = True
                                        # Process any remaining LM results before breaking
                                        if self.lm_processor:
                                            self._process_lm_results()
                                        # Break immediately - don't continue to next iteration
                                        break
                                    else:
                                        logger.debug(f"Updated position to {self.current_time_position:.1f}s (segment ended at {segment_end_time:.1f}s, last_timestamp: {self.subtitle_extractor.last_timestamp:.1f}s)")
                        else:
                            logger.debug("No new subtitle segments")
                            # For non-live videos in chunked mode, if no new segments, we've reached the end
                            if not self.is_live_video and self.process_mode == "chunked":
                                # Mark that all subtitles have been processed
                                self.subtitles_fully_processed = True
                                # Update current_time_position to match subtitle extractor's position
                                if self.subtitle_extractor.last_timestamp > 0:
                                    self.current_time_position = self.subtitle_extractor.last_timestamp
                                    # Mark that we've reached the end
                                    if self.video_duration:
                                        self.current_time_position = self.video_duration
                                    logger.info(f"No new subtitle segments available. Reached end of video (last_timestamp: {self.subtitle_extractor.last_timestamp:.1f}s).")
                                else:
                                    # If last_timestamp is still 0, something went wrong, but break anyway
                                    logger.warning("No subtitle segments and last_timestamp is 0. Ending processing.")
                                # Process any remaining LM results before breaking
                                if self.lm_processor:
                                    self._process_lm_results()
                                # Break immediately - don't continue to next iteration
                                break
                            
                    except Exception as e:
                        logger.error(f"Error fetching subtitles: {e}")
                        import traceback
                        logger.debug(traceback.format_exc())
                        if self.args.method == "subtitles":
                            # If subtitles-only mode fails, wait and retry
                            time.sleep(self.args.interval)
                            continue
                        else:
                            # In auto mode, fallback to Whisper
                            logger.info("Falling back to Whisper due to subtitle extraction error")
                            self.use_subtitles = False
                            self.use_whisper = True
                
                # Use Whisper if subtitles not available, failed, or returned no segments
                # But don't use Whisper if we're breaking after LM (for non-live videos with subtitles)
                # Also don't use Whisper if all subtitles have been fully processed
                should_use_whisper = (
                    self.use_whisper and 
                    self.whisper_transcriber and 
                    (not self.use_subtitles or not subtitle_success) and
                    not self.subtitles_fully_processed  # Don't use Whisper if all subtitles are processed
                )
                
                if should_use_whisper:
                    # Additional safeguard: never use Whisper if subtitles are fully processed
                    if self.subtitles_fully_processed:
                        logger.debug("Skipping Whisper: all subtitle segments have been processed")
                    else:
                        try:
                            # If we have a chunk from previous iteration, add it to transcription queue
                            if next_audio_path:
                                logger.debug(f"Adding chunk #{self.iteration-1} to transcription queue")
                                self.transcription_queue.put((
                                    next_audio_path,
                                    self.iteration - 1,
                                    timestamp,
                                    next_extract_time,
                                    self.last_chunk_end_text,
                                    self.last_chunk_end_translated
                                ))
                                next_audio_path = None
                            
                            # Start downloading next chunk immediately (parallel with transcription)
                            extract_start = time.time()
                            logger.info("Extracting audio chunk...")
                            
                            # For non-live videos in chunked mode, use start_time
                            if not self.is_live_video and self.process_mode == "chunked":
                                # Check if we've reached the end of the video
                                if self.video_duration and self.current_time_position >= self.video_duration:
                                    logger.info("Reached end of video")
                                    break
                                
                                # Extract chunk starting at current_time_position
                                chunk_duration = min(self.args.interval, self.video_duration - self.current_time_position if self.video_duration else self.args.interval)
                                audio_path = self.whisper_transcriber.extract_audio_from_youtube(
                                    self.video_id,
                                    duration=chunk_duration,
                                    overlap=0,
                                    start_time=self.current_time_position
                                )
                                extract_time = time.time() - extract_start
                                # Update position for next chunk (account for overlap)
                                self.current_time_position += chunk_duration - self.overlap_seconds
                            else:
                                # For live streams, use normal extraction
                                audio_path = self.whisper_transcriber.extract_audio_from_youtube(
                                    self.video_id,
                                    duration=self.args.interval,
                                    overlap=self.overlap_seconds
                                )
                                extract_time = time.time() - extract_start
                            
                            # Check if audio extraction failed
                            if not audio_path:
                                error_msg = f"\n[ERROR] Failed to extract audio chunk at {self.current_time_position:.1f}s. Skipping this chunk.\n"
                                logger.error(f"Audio extraction failed at {self.current_time_position:.1f}s")
                                print(error_msg, flush=True)  # Send error to Telegram via stdout capture
                                # Wait a bit before retrying next chunk
                                time.sleep(2)
                                continue
                            
                            # For first chunk, transcribe immediately (no previous chunk to overlap with)
                            if first_chunk and audio_path:
                                logger.info("Transcribing first chunk...")
                                text, detected_language, language_prob = self.whisper_transcriber.transcribe_audio(audio_path)
                                
                                if text.strip():
                                    # Reset failure counter on successful transcription
                                    consecutive_failures = 0
                                    if detected_language != 'ru':
                                        translated_text = self.whisper_transcriber.translate_to_russian(text, detected_language)
                                    else:
                                        translated_text = text
                                    
                                    # Store last N words for overlap detection with next chunk
                                    words_to_store = max(4, int(self.overlap_seconds * 2))
                                    text_words = text.split()
                                    trans_words = translated_text.split()
                                    
                                    if len(text_words) >= words_to_store:
                                        self.last_chunk_end_text = " ".join(text_words[-words_to_store:])
                                        self.last_chunk_end_translated = " ".join(trans_words[-words_to_store:]) if len(trans_words) >= words_to_store else translated_text
                                    else:
                                        self.last_chunk_end_text = text
                                        self.last_chunk_end_translated = translated_text
                                    
                                    # Store in cumulative list
                                    self.all_transcribed_text.append({
                                        'timestamp': timestamp,
                                        'text': text,
                                        'translated': translated_text,
                                        'iteration': 1,
                                        'language': detected_language,
                                        'language_prob': language_prob
                                    })
                                    
                                    # Check if transcription is duplicate (same as last one) - break if so
                                    if self.last_transcription_text is not None and text.strip() == self.last_transcription_text.strip():
                                        logger.info(f"Duplicate transcription detected (same as last): '{text[:50]}...'. Breaking loop.")
                                        # Process any remaining LM results before breaking
                                        if self.lm_processor:
                                            self._process_lm_results()
                                        break
                                    self.last_transcription_text = text
                                    
                                    # Add to sentence buffers
                                    self.text_buffer += " " + text if self.text_buffer else text
                                    self.translated_buffer += " " + translated_text if self.translated_buffer else translated_text
                                    
                                    # Extract complete sentences
                                    sentence_pattern = r'[^.!?]*[.!?]\s*'
                                    complete_sentences = re.findall(sentence_pattern, self.text_buffer)
                                    complete_translated_sentences = re.findall(sentence_pattern, self.translated_buffer)
                                    
                                    if complete_sentences:
                                        output_text = "".join(complete_sentences).strip()
                                        output_translated = "".join(complete_translated_sentences).strip()
                                        
                                        # Remove complete sentences from buffer
                                        complete_length = sum(len(s) for s in complete_sentences)
                                        self.text_buffer = self.text_buffer[complete_length:].strip()
                                        
                                        complete_translated_length = sum(len(s) for s in complete_translated_sentences)
                                        self.translated_buffer = self.translated_buffer[complete_translated_length:].strip()
                                        
                                        # Output transcription
                                        self._output_transcription(
                                            timestamp,
                                            output_text,
                                            output_translated,
                                            detected_language,
                                            language_prob,
                                            iteration=1
                                        )
                                        
                                        # Send to LM queue if enabled and interval has elapsed
                                        if self.lm_processor and self.lm_queue:
                                            current_time = time.time()
                                            if current_time - self.last_lm_call_time >= self.lm_interval:
                                                self.lm_queue.put((translated_text, timestamp, {
                                                    'iteration': 1,
                                                    'language': detected_language
                                                }))
                                                self.last_lm_call_time = current_time
                                                if self.debug_mode:
                                                    logger.debug(f"Added first chunk to LM queue (interval: {self.lm_interval}s)")
                                            else:
                                                time_since_last = current_time - self.last_lm_call_time
                                                if self.debug_mode:
                                                    logger.debug(f"Skipping LM queue (interval not elapsed: {time_since_last:.1f}s < {self.lm_interval}s)")
                                
                                first_chunk = False
                                next_audio_path = None
                            else:
                                # Store for next iteration (will be transcribed while we download next chunk)
                                # Only store if audio_path is valid
                                if audio_path:
                                    next_audio_path = audio_path
                                    next_extract_time = extract_time
                                else:
                                    # Audio extraction failed, log and skip
                                    consecutive_failures += 1
                                    error_msg = f"\n[ERROR] Failed to extract audio chunk ({consecutive_failures}/{MAX_CONSECUTIVE_FAILURES}). Skipping this chunk.\n"
                                    logger.error(f"Audio extraction failed, skipping chunk (consecutive failures: {consecutive_failures})")
                                    print(error_msg, flush=True)  # Send error to Telegram via stdout capture
                                    
                                    # Stop if too many consecutive failures
                                    if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                                        error_msg = f"\n[ERROR] Too many consecutive failures ({consecutive_failures}). Stopping transcription.\n"
                                        logger.error(f"Stopping transcription due to {consecutive_failures} consecutive failures")
                                        print(error_msg, flush=True)
                                        break
                                    
                                    next_audio_path = None
                                first_chunk = False
                                
                                # Reset failure counter on success
                                if audio_path:
                                    consecutive_failures = 0
                            
                            # Process completed transcriptions
                            should_break = self._process_transcription_results()
                            if should_break:
                                logger.info("Breaking loop due to duplicate transcription")
                                break
                            
                        except Exception as e:
                            consecutive_failures += 1
                            logger.error(f"Error with Whisper transcription: {e} (consecutive failures: {consecutive_failures})")
                            
                            # Stop if too many consecutive failures
                            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                                error_msg = f"\n[ERROR] Too many consecutive failures ({consecutive_failures}). Stopping transcription.\n"
                                logger.error(f"Stopping transcription due to {consecutive_failures} consecutive failures")
                                print(error_msg, flush=True)
                                break
                            
                            # Continue to next iteration even if this one failed
                
                # Process LM results
                if self.lm_processor:
                    self._process_lm_results()
                
                # Break if we've processed all subtitle segments (for non-live videos)
                # This check must happen BEFORE the continue statement below
                # Note: We should have already broken earlier when segments were processed,
                # but this is a safety check in case we reach here
                if not self.is_live_video and self.process_mode == "chunked" and self.subtitles_fully_processed:
                    logger.info("Breaking after processing all subtitle segments")
                    break
                
                # Calculate timing metrics and display
                if should_use_whisper and len(self.timing_history) >= 2:
                    avg_processing = sum(t[0] for t in self.timing_history) / len(self.timing_history)
                    avg_chunk_duration = sum(t[1] for t in self.timing_history) / len(self.timing_history)
                    overhead = avg_processing - avg_chunk_duration
                    
                    # Calculate optimal interval
                    optimal_interval = max(self.args.interval, int(avg_chunk_duration + overhead + 5))
                    
                    logger.info(f"📊 Overhead: {overhead:.1f}s | Current interval: {self.args.interval}s | Optimal: {optimal_interval}s")
                    
                    # Warn if falling behind
                    if avg_processing > self.args.interval:
                        logger.warning(f"⚠️  Falling behind! Avg processing: {avg_processing:.1f}s, interval: {self.args.interval}s")
                        logger.warning(f"💡 Consider using --interval {optimal_interval} for better real-time performance")
                
                # For non-live videos in chunked mode, check if we've reached the end
                if not self.is_live_video and self.process_mode == "chunked":
                    # Check if we should break before continuing
                    # Note: We should have already broken earlier when segments were processed,
                    # but this is a safety check in case we reach here
                    if self.subtitles_fully_processed:
                        logger.info("Breaking: all subtitle segments processed")
                        break
                    
                    if self.video_duration and self.current_time_position >= self.video_duration:
                        logger.info("Reached end of video. Processing remaining chunks...")
                        break
                    
                    # For non-live videos in chunked mode, skip sleep - process continuously
                    continue  # Skip the sleep and go to next iteration
                
                # Calculate actual cycle time and adjust sleep (only for live streams)
                cycle_time = time.time() - cycle_start_time
                sleep_time = max(0, self.args.interval - cycle_time)
                
                if sleep_time > 0:
                    if not self.shutdown_flag():
                        # Make sleep interruptible by sleeping in small chunks
                        sleep_chunk = 0.5  # Check shutdown_flag every 0.5 seconds
                        slept = 0
                        while slept < sleep_time and not self.shutdown_flag():
                            remaining = min(sleep_chunk, sleep_time - slept)
                            time.sleep(remaining)
                            slept += remaining
                else:
                    logger.warning(f"⚠️  Cycle took {cycle_time:.1f}s, no sleep time available (interval: {self.args.interval}s)")
            
            # Process any remaining chunks in queue
            if should_use_whisper:
                logger.info("Processing remaining chunks...")
                if next_audio_path:
                    self.transcription_queue.put((
                        next_audio_path,
                        self.iteration,
                        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        next_extract_time,
                        self.last_chunk_end_text,
                        self.last_chunk_end_translated
                    ))
            
            # Wait for transcription queue to empty (with timeout to check shutdown_flag)
            if should_use_whisper:
                # Wait for queue to empty, but check shutdown_flag periodically
                max_wait_time = 5.0  # Maximum time to wait for queue to empty
                wait_start = time.time()
                while not self.transcription_queue.empty() and not self.shutdown_flag():
                    if time.time() - wait_start > max_wait_time:
                        logger.debug("Timeout waiting for queue to empty, continuing...")
                        break
                    time.sleep(0.1)
                # If shutdown requested, send None to worker to stop it
                if self.shutdown_flag():
                    try:
                        self.transcription_queue.put(None)
                    except:
                        pass
            
            # Process any remaining results
            while not self.result_queue.empty():
                should_break = self._process_transcription_results()
                if should_break:
                    break
            
            # Process any remaining LM results
            if self.lm_processor:
                while not self.lm_result_queue.empty():
                    self._process_lm_results()
            
            # Output any remaining incomplete text at the end
            if self.text_buffer.strip():
                logger.info(f"Final incomplete text: {self.text_buffer}")
                timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                if self.args.translation_output == "russian-only":
                    print(f"\n{self.translated_buffer}\n", flush=True)
                elif self.args.translation_output == "both":
                    print(f"\n{self.text_buffer}\n[RU] {self.translated_buffer}\n", flush=True)
                else:
                    print(f"\n{self.text_buffer}\n", flush=True)
        
        except KeyboardInterrupt:
            logger.info("\nInterrupted by user")
        except Exception as e:
            logger.exception(f"Unexpected error: {e}")
        finally:
            # Send shutdown signal to workers
            if self.use_whisper and self.whisper_transcriber and self.transcription_queue:
                try:
                    self.transcription_queue.put(None)  # Shutdown signal
                    # Give worker a moment to finish
                    time.sleep(0.5)
                except:
                    pass
            
            if self.lm_processor and self.lm_queue:
                try:
                    self.lm_queue.put(None)  # Shutdown signal
                    # Give worker a moment to finish
                    time.sleep(0.5)
                except:
                    pass
            
            logger.info("Transcription stopped. Goodbye!")

