"""
Utility functions for Telegram bot
Handles authentication, token management, metadata, and file operations
"""

import json
import os
import threading
import logging
from typing import Optional, List, Dict, Tuple
from datetime import datetime, timezone

# Try to load environment variables from .env file
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv not installed, skip

logger = logging.getLogger(__name__)

# Thread lock for token management
_token_lock = threading.Lock()

# Daily capacity constants
DAILY_CAPACITY = 1000
WARNING_THRESHOLD = 50


def load_metadata(metadata_path: str = None) -> Dict:
    """Load metadata from JSON file (with optional env var override)"""
    if metadata_path is None:
        metadata_path = os.getenv("METADATA_PATH", "metadata.json")
    
    try:
        with open(metadata_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        logger.error(f"Metadata file not found: {metadata_path}")
        raise
    except json.JSONDecodeError as e:
        logger.error(f"Error parsing metadata.json: {e}")
        raise


def save_metadata(metadata: Dict, metadata_path: str = "metadata.json"):
    """Save metadata to JSON file"""
    try:
        with open(metadata_path, 'w', encoding='utf-8') as f:
            json.dump(metadata, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Error saving metadata.json: {e}")
        raise


def get_text(metadata: Dict, key: str, language: str = None) -> str:
    """Get text from metadata by key, using default language if not specified"""
    if language is None:
        language = metadata.get("default_language", "ru")
    
    try:
        return metadata["languages"][language][key]
    except KeyError:
        # Fallback to default language
        default_lang = metadata.get("default_language", "ru")
        try:
            return metadata["languages"][default_lang][key]
        except KeyError:
            logger.warning(f"Text key '{key}' not found in metadata")
            return key


def load_auth_key(key_path: str = "key.txt") -> str:
    """Load authentication key from environment variable or file"""
    # Try environment variable first
    auth_key = os.getenv("AUTH_KEY")
    if auth_key:
        return auth_key.strip()
    
    # Fallback to file
    try:
        with open(key_path, 'r', encoding='utf-8') as f:
            return f.read().strip()
    except FileNotFoundError:
        logger.error(f"Key file not found: {key_path} and AUTH_KEY environment variable not set")
        raise


def is_user_authenticated(user_id: int, config_path: str = None) -> bool:
    """Check if user ID is in allowed list (from config.json)"""
    if config_path is None:
        config_path = os.getenv("CONFIG_PATH", "config.json")
    
    try:
        config = load_config(config_path)
        allowed_ids = config.get("allowed_user_ids", [])
        return user_id in allowed_ids
    except Exception as e:
        logger.error(f"Error checking authentication: {e}")
        return False


def add_authenticated_user(user_id: int, config_path: str = None):
    """
    Add user ID to allowed list in config.json with file locking to prevent race conditions.
    
    This function uses file locking to ensure atomic read-modify-write operations,
    preventing data loss when multiple users authenticate concurrently.
    """
    if config_path is None:
        config_path = os.getenv("CONFIG_PATH", "config.json")
    
    import tempfile
    
    # Import platform-specific locking
    try:
        import fcntl
        has_fcntl = True
    except ImportError:
        # Windows doesn't have fcntl
        has_fcntl = False
    
    # Use a lock file to ensure atomic operations
    lock_path = config_path + ".lock"
    
    try:
        # Create lock file if it doesn't exist
        lock_file = open(lock_path, 'w')
        
        try:
            # Acquire exclusive lock (blocks until available)
            if has_fcntl:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                logger.debug(f"Acquired lock for config file (fcntl)")
            else:
                # Windows: use file existence as lock (not perfect but better than nothing)
                import time
                retry_count = 0
                while os.path.exists(lock_path + ".active") and retry_count < 50:
                    time.sleep(0.1)
                    retry_count += 1
                open(lock_path + ".active", 'w').close()
                logger.debug(f"Acquired lock for config file (file-based)")
            
            # Now safely read, modify, and write
            config = load_config(config_path)
            
            # Check if already added (inside lock to ensure accuracy)
            if "allowed_user_ids" not in config:
                config["allowed_user_ids"] = []
            
            if user_id in config["allowed_user_ids"]:
                logger.info(f"User {user_id} is already authenticated")
                return
            
            # Add user ID
            config["allowed_user_ids"].append(user_id)
            
            # Atomic write: write to temp file, then rename
            temp_fd, temp_path = tempfile.mkstemp(
                dir=os.path.dirname(config_path),
                prefix=".config_",
                suffix=".tmp"
            )
            
            try:
                with os.fdopen(temp_fd, 'w', encoding='utf-8') as f:
                    json.dump(config, f, indent=2, ensure_ascii=False)
                
                # Atomic rename (replaces old file)
                os.replace(temp_path, config_path)
                logger.info(f"Added user {user_id} to allowed list in config.json")
                
            except Exception as e:
                # Clean up temp file on error
                if os.path.exists(temp_path):
                    os.remove(temp_path)
                raise
            
        finally:
            # Always release lock
            if has_fcntl:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            else:
                # Windows: remove active lock file
                if os.path.exists(lock_path + ".active"):
                    os.remove(lock_path + ".active")
            lock_file.close()
            logger.debug(f"Released lock for config file")
            
    except Exception as e:
        logger.error(f"Error adding user to allowed list: {e}")
        raise
    finally:
        # Clean up lock file if it exists and is empty
        try:
            if os.path.exists(lock_path) and os.path.getsize(lock_path) == 0:
                os.remove(lock_path)
        except Exception:
            pass  # Ignore cleanup errors


def load_config(config_path: str = None) -> Dict:
    """Load configuration from JSON file (with optional env var override)"""
    if config_path is None:
        config_path = os.getenv("CONFIG_PATH", "config.json")
    
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        logger.error(f"Config file not found: {config_path}")
        raise
    except json.JSONDecodeError as e:
        logger.error(f"Error parsing config.json: {e}")
        raise


def save_config(config: Dict, config_path: str = "config.json"):
    """Save configuration to JSON file"""
    try:
        with open(config_path, 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Error saving config.json: {e}")
        raise


def update_context(context_text: str, config_path: str = "config.json"):
    """Update context field in config.json"""
    config = load_config(config_path)
    config["context"] = context_text
    save_config(config, config_path)


def reset_context(config_path: str = "config.json"):
    """Reset context to empty string in config.json"""
    update_context("", config_path)


def load_tokens(api_key_path: str = "api_key.txt") -> List[str]:
    """Load tokens from environment variable or file"""
    # Try environment variable first
    api_key = os.getenv("OPENROUTER_API_KEY")
    if api_key:
        return [api_key.strip()]
    
    # Fallback to file
    if not os.path.exists(api_key_path):
        return []
    
    try:
        with open(api_key_path, 'r', encoding='utf-8') as f:
            tokens = [line.strip() for line in f if line.strip()]
            return tokens
    except Exception as e:
        logger.error(f"Error loading tokens: {e}")
        return []


def save_tokens(tokens: List[str], api_key_path: str = "api_key.txt"):
    """Save tokens to api_key.txt (one per line)"""
    try:
        with open(api_key_path, 'w', encoding='utf-8') as f:
            for token in tokens:
                f.write(f"{token}\n")
    except Exception as e:
        logger.error(f"Error saving tokens: {e}")
        raise


def add_tokens(new_tokens: List[str], api_key_path: str = "api_key.txt", metadata_path: str = "metadata.json"):
    """Add new tokens to api_key.txt only. Tokens are never stored in JSON."""
    # Load existing tokens
    existing_tokens = load_tokens(api_key_path)
    
    # Add new tokens
    for token in new_tokens:
        token = token.strip()
        if token and token not in existing_tokens:
            existing_tokens.append(token)
    
    # Save tokens to file only (not in JSON)
    save_tokens(existing_tokens, api_key_path)
    
    # Note: Capacity is now tracked by date in metadata.json (daily_capacity field)
    # No need to initialize token_capacities anymore


def get_current_token(api_key_path: str = "api_key.txt", metadata_path: str = "metadata.json") -> Optional[str]:
    """
    Get current active token (first token in file).
    Returns token string or None if no tokens available.
    Capacity is now tracked separately by date.
    """
    tokens = load_tokens(api_key_path)
    if not tokens:
        return None
    return tokens[0]


def get_daily_capacity(metadata_path: str = "metadata.json") -> int:
    """
    Get current daily capacity, resetting to 1000 if it's a new day (UTC).
    Returns current capacity.
    """
    with _token_lock:
        metadata = load_metadata(metadata_path)
        
        # Get current UTC date
        today_utc = datetime.now(timezone.utc).date().isoformat()
        
        # Check if we have capacity tracking
        if "daily_capacity" not in metadata:
            metadata["daily_capacity"] = DAILY_CAPACITY
            metadata["last_capacity_date"] = today_utc
            save_metadata(metadata, metadata_path)
            return DAILY_CAPACITY
        
        last_date = metadata.get("last_capacity_date")
        
        # If it's a new day, reset capacity
        if last_date != today_utc:
            metadata["daily_capacity"] = DAILY_CAPACITY
            metadata["last_capacity_date"] = today_utc
            save_metadata(metadata, metadata_path)
            return DAILY_CAPACITY
        
        # Return current capacity
        return metadata.get("daily_capacity", DAILY_CAPACITY)


def decrement_daily_capacity(metadata_path: str = "metadata.json") -> bool:
    """
    Decrement daily capacity by 1 (called for each LM API call).
    Returns True if capacity still available, False if exhausted.
    Automatically resets to 1000 if it's a new day (UTC).
    """
    with _token_lock:
        metadata = load_metadata(metadata_path)
        
        # Get current UTC date
        today_utc = datetime.now(timezone.utc).date().isoformat()
        
        # Initialize if not exists
        if "daily_capacity" not in metadata:
            metadata["daily_capacity"] = DAILY_CAPACITY
            metadata["last_capacity_date"] = today_utc
        
        last_date = metadata.get("last_capacity_date")
        
        # If it's a new day, reset capacity
        if last_date != today_utc:
            metadata["daily_capacity"] = DAILY_CAPACITY
            metadata["last_capacity_date"] = today_utc
        
        # Get current capacity
        capacity = metadata.get("daily_capacity", DAILY_CAPACITY)
        
        if capacity <= 0:
            return False
        
        # Decrement capacity
        capacity -= 1
        metadata["daily_capacity"] = capacity
        
        # Update metadata
        save_metadata(metadata, metadata_path)
        
        return capacity > 0


def check_capacity_at_start(metadata_path: str = "metadata.json") -> Tuple[int, bool]:
    """
    Check capacity at the start of processing (when link/video is sent).
    Returns (current_capacity, should_warn) tuple.
    Automatically resets to 1000 if it's a new day (UTC).
    """
    capacity = get_daily_capacity(metadata_path)
    should_warn = capacity < WARNING_THRESHOLD
    return (capacity, should_warn)


def get_token_count(api_key_path: str = "api_key.txt") -> int:
    """Get count of available tokens"""
    tokens = load_tokens(api_key_path)
    return len(tokens)


def check_token_warning(metadata_path: str = "metadata.json") -> bool:
    """Check if daily capacity is less than warning threshold (50)"""
    capacity = get_daily_capacity(metadata_path)
    return capacity < WARNING_THRESHOLD

