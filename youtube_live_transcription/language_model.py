"""
Language Model Processor Module

Processes Russian transcriptions through a language model (HuggingFace) to generate reports.
Runs asynchronously in a background worker thread.
"""

import math
import logging
import sys
import os
from typing import List, Dict, Optional, Callable
from datetime import datetime

# Add project root to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logger = logging.getLogger(__name__)


class LMProcessor:
    """
    Processes Russian transcriptions through a language model.
    Maintains a sliding window of transcriptions and generates reports.
    """
    
    def __init__(
        self,
        api_key_file: str = "api_key.txt",
        prompt_file: str = "prompt.md",
        model: str = "google/gemma-3-27b-it:free",
        window_seconds: int = 120,
        interval: int = 30,
        context: str = "",
        shutdown_flag: Optional[Callable[[], bool]] = None
    ):
        """
        Initialize LM processor.
        
        Args:
            api_key_file: Path to file containing OpenRouter.ai API key
            prompt_file: Path to file containing the prompt
            model: OpenRouter.ai model name
            window_seconds: Window size n for LM processing (default: 120)
            interval: Interval between transcriptions in seconds (default: 30)
            context: Additional context to add to prompt (default: empty string)
            shutdown_flag: Optional callable to check for shutdown signal
        """
        self.api_key_file = api_key_file
        self.prompt_file = prompt_file
        self.model = model
        self.window_seconds = window_seconds
        self.interval = interval
        self.context = context
        self.shutdown_flag = shutdown_flag
        
        # Initialize OpenAI client
        self.client = None
        self.prompt = None
        self._initialize_client()
        self._load_prompt()
    
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
                from telegram_bot_utils import (
                    get_current_token,
                    check_token_warning,
                    load_metadata,
                    get_text
                )
                use_token_management = True
            except ImportError:
                # Fallback to old method if utils not available
                use_token_management = False
                logger.warning("Token management utilities not available, using direct file read")
            
            if use_token_management:
                # Use token management system
                api_key = get_current_token(self.api_key_file, "metadata.json")
                if api_key is None:
                    # Try to load metadata for error message
                    try:
                        metadata = load_metadata("metadata.json")
                        error_msg = get_text(metadata, "no_tokens_available")
                        logger.error(error_msg)
                    except:
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
    
    def _load_prompt(self):
        """Load prompt from file and add context if provided"""
        try:
            with open(self.prompt_file, "r", encoding="utf-8") as f:
                self.prompt = f.read()
            
            # Add context if provided
            if self.context and self.context.strip():
                self.prompt += f"\nДополнительный контекст: {self.context}"
                logger.info(f"Prompt loaded from {self.prompt_file} with additional context")
            else:
                logger.info(f"Prompt loaded from {self.prompt_file}")
        except FileNotFoundError:
            logger.error(f"Prompt file not found: {self.prompt_file}")
            raise
        except Exception as e:
            logger.error(f"Error loading prompt: {e}")
            raise
    
    def _calculate_window_size(self) -> int:
        """
        Calculate the number of transcriptions to include in the window.
        Returns ceil(window_seconds / interval)
        """
        return math.ceil(self.window_seconds / self.interval)
    
    def _get_window_text(self, all_transcriptions: List[Dict]) -> str:
        """
        Extract the last N transcriptions (where N = ceil(window_seconds / interval))
        and concatenate their Russian text.
        
        Args:
            all_transcriptions: List of transcription dicts with 'translated' key
        
        Returns:
            Concatenated Russian text from the window
        """
        if not all_transcriptions:
            return ""
        
        window_size = self._calculate_window_size()
        # Get last N transcriptions (or all if fewer available)
        window_transcriptions = all_transcriptions[-window_size:] if len(all_transcriptions) > window_size else all_transcriptions
        
        # Concatenate Russian text
        texts = [t.get('translated', '') for t in window_transcriptions if t.get('translated', '').strip()]
        return " ".join(texts)
    
    def _get_token_and_decrement(self):
        """Get current token and decrement daily capacity"""
        try:
            from telegram_bot_utils import (
                get_current_token,
                decrement_daily_capacity,
                load_metadata,
                get_text
            )
            
            # Get current token
            token = get_current_token(self.api_key_file, "metadata.json")
            if token is None:
                try:
                    metadata = load_metadata("metadata.json")
                    error_msg = get_text(metadata, "no_tokens_available")
                    logger.error(error_msg)
                except:
                    logger.error("No tokens available")
                return None
            
            # Decrement daily capacity (called for each LM API call)
            has_capacity = decrement_daily_capacity("metadata.json")
            
            if not has_capacity:
                # Daily capacity exhausted
                logger.warning("Daily capacity exhausted. LM processing will fail.")
                # Still return token, but capacity check will prevent further calls
            
            return token
        except ImportError:
            # Token management not available, return None to use existing client
            return None
    
    def process_transcription_window(self, all_transcriptions: List[Dict]) -> Optional[str]:
        """
        Process a window of transcriptions through the language model.
        
        Args:
            all_transcriptions: List of transcription dicts with 'translated' key
        
        Returns:
            LM-generated report text, or None if processing failed
        """
        if self.client is None or self.prompt is None:
            logger.error("LM client or prompt not initialized")
            return None
        
        # Get window text
        window_text = self._get_window_text(all_transcriptions)
        
        if not window_text.strip():
            logger.debug("No text in window to process")
            return None
        
        max_retries = 3
        for attempt in range(max_retries):
            try:
                # Combine prompt and text
                content = self.prompt + "\n\n" + window_text
                
                logger.info(f"Processing {len(all_transcriptions)} transcriptions through LM (window: {self._calculate_window_size()})...")
                
                # Update token before API call (decrement capacity)
                token = self._get_token_and_decrement()
                if token is None and attempt == 0:
                    # Try to reinitialize client if token management failed
                    try:
                        from telegram_bot_utils import get_current_token
                        token = get_current_token(self.api_key_file, "metadata.json")
                        if token:
                            from openai import OpenAI
                            self.client = OpenAI(
                                base_url="https://openrouter.ai/api/v1",
                                api_key=token,
                            )
                    except:
                        pass
                
                # Call LM API
                completion = self.client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "text",
                                    "text": content
                                }
                            ]
                        }
                    ],
                )
                
                report = completion.choices[0].message.content.strip('\n')
                
                if report:
                    logger.info("LM report generated successfully")
                    return report
                else:
                    logger.warning("LM returned empty report")
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
                    logger.error(f"Error processing transcription window through LM: {e}")
                    import traceback
                    logger.debug(traceback.format_exc())
                    return None
        
        return None
    
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
                except:
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
                    except:
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
                    except:
                        pass
        
        # Clean up any remaining items in queue on shutdown
        logger.debug("LM worker shutting down...")
        while not lm_queue.empty():
            try:
                item = lm_queue.get_nowait()
                if item is not None:
                    lm_queue.task_done()
            except:
                break
        
        logger.info("LM worker thread stopped")

