"""
YouTube Subtitle Extractor Module

Extracts auto-generated subtitles from YouTube live streams.
Uses youtube-transcript-api to fetch available subtitles.
"""

import logging
from typing import List, Dict, Tuple

logger = logging.getLogger(__name__)


class YouTubeSubtitleExtractor:
    """
    Extracts auto-generated subtitles from YouTube live streams.
    Uses youtube-transcript-api to fetch available subtitles.
    """
    
    def __init__(self, video_id: str):
        self.video_id = video_id
        self.last_segment_index = 0
        self.last_timestamp = 0.0
        
    def extract_video_id(self, url_or_id: str) -> str:
        """Extract video ID from YouTube URL or return if already an ID"""
        if "youtube.com" in url_or_id or "youtu.be" in url_or_id:
            # Extract from URL
            if "v=" in url_or_id:
                video_id = url_or_id.split("v=")[-1].split("&")[0]
            elif "youtu.be/" in url_or_id:
                video_id = url_or_id.split("youtu.be/")[-1].split("?")[0]
            else:
                raise ValueError(f"Could not extract video ID from URL: {url_or_id}")
            return video_id
        return url_or_id
    
    def check_subtitles_available(self) -> Tuple[bool, List[str]]:
        """
        Check if subtitles are available for the video.
        Returns: (available, list of available languages)
        """
        try:
            from youtube_transcript_api import YouTubeTranscriptApi
            from youtube_transcript_api._errors import TranscriptsDisabled, NoTranscriptFound
            
            available_languages = []
            
            # Try to use list_transcripts if available (newer API)
            try:
                transcript_list = YouTubeTranscriptApi.list_transcripts(self.video_id)
                
                # Check for manually created transcripts
                for transcript in transcript_list:
                    if not transcript.is_generated:
                        available_languages.append(transcript.language_code)
                
                # Check for auto-generated transcripts
                for transcript in transcript_list:
                    if transcript.is_generated:
                        lang_label = f"{transcript.language_code} (auto-generated)"
                        if lang_label not in available_languages:
                            available_languages.append(lang_label)
                
                # If we found transcripts via list_transcripts, return them
                if available_languages:
                    return True, available_languages
                    
            except (AttributeError, NoTranscriptFound):
                # Fallback: try to get transcript directly to check availability
                pass
            
            # Fallback: try direct access with common languages
            for lang in ['en', 'en-US', 'en-GB', 'ru', 'es', 'fr', 'de']:
                try:
                    YouTubeTranscriptApi.get_transcript(self.video_id, languages=[lang])
                    available_languages.append(lang)
                    break  # Found at least one, that's enough
                except:
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
            from youtube_transcript_api._errors import TranscriptsDisabled, NoTranscriptFound
            
            # Get transcript
            try:
                transcript = YouTubeTranscriptApi.get_transcript(self.video_id, languages=[language])
            except NoTranscriptFound:
                # Try auto-generated
                try:
                    transcript_list = YouTubeTranscriptApi.list_transcripts(self.video_id)
                    transcript_data = transcript_list.find_generated_transcript([language])
                    transcript = transcript_data.fetch()
                except NoTranscriptFound:
                    # Try any available language
                    try:
                        transcript_list = YouTubeTranscriptApi.list_transcripts(self.video_id)
                        # Get first available transcript (manual or auto-generated)
                        transcript_data = transcript_list.find_transcript(['en', 'en-US', 'en-GB'])
                        transcript = transcript_data.fetch()
                        logger.info(f"Using transcript in language: {transcript_data.language_code}")
                    except:
                        # Try to get any available transcript
                        try:
                            transcript_list = YouTubeTranscriptApi.list_transcripts(self.video_id)
                            for transcript_item in transcript_list:
                                transcript = transcript_item.fetch()
                                logger.info(f"Using transcript in language: {transcript_item.language_code}")
                                break
                        except Exception as e2:
                            logger.error(f"Could not find any transcript: {e2}")
                            raise NoTranscriptFound(self.video_id)
            
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

