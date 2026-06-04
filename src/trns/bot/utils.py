"""
Utility functions for Telegram bot
Handles authentication, token management, metadata, and file operations
"""

import json
import logging
import os
import tempfile
import threading
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

# Try to load environment variables from .env file
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv not installed, skip

logger = logging.getLogger(__name__)

# Base directory for resolving relative resource paths
_trns_home = os.path.abspath(os.environ.get("TRNS_HOME", os.getcwd()))

# Thread lock for token management
_token_lock = threading.Lock()

# Thread lock for user settings read-modify-write
_settings_lock = threading.Lock()

# Daily capacity constants
DAILY_CAPACITY = 1000
WARNING_THRESHOLD = 50

_DEFAULT_USER_SETTINGS_PATH = "user_settings.json"
_DEFAULT_CAPACITY_STATE_NAME = "global.json"


def _resolve_path(path: str) -> str:
    """Resolve a relative path against TRNS_HOME."""
    if path and not os.path.isabs(path):
        return os.path.join(_trns_home, path)
    return path


def _state_dir() -> str:
    """Return the runtime state directory, resolved under TRNS_HOME when relative."""
    configured = os.environ.get("TRNS_STATE_DIR", "state")
    return _resolve_path(configured)


def _state_path(*parts: str) -> str:
    """Return a path below the runtime state directory."""
    return os.path.join(_state_dir(), *parts)


def _user_state_path(user_id: int) -> str:
    """Return the per-user state file path for a Telegram user."""
    return _state_path("users", f"{int(user_id)}.json")


def _default_capacity_path() -> str:
    """Return the global runtime capacity state file path."""
    return _state_path(_DEFAULT_CAPACITY_STATE_NAME)


def _write_json_atomic(path: str, data: Dict):
    """Write JSON atomically so partial writes do not corrupt state files."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=os.path.dirname(path) or ".",
        prefix=f".{os.path.basename(path)}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp_path, path)
    except Exception:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


def _load_json_file(path: str, default: Optional[Dict] = None) -> Dict:
    """Load a JSON object from disk, returning a shallow default when absent."""
    if not os.path.exists(path):
        return dict(default or {})
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict):
        return data
    logger.warning("Expected JSON object in %s, got %s", path, type(data).__name__)
    return dict(default or {})


def load_metadata(metadata_path: str = None) -> Dict:
    """Load metadata from JSON file (with optional env var override).

    If the file does not exist, returns a minimal default so the bot can
    start up (with English-only placeholder strings) instead of crashing.
    """
    if metadata_path is None:
        metadata_path = os.getenv("METADATA_PATH", "metadata.json")
    metadata_path = _resolve_path(metadata_path)

    try:
        with open(metadata_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        logger.warning(
            f"Metadata file not found: {metadata_path}. "
            "Using minimal built-in defaults. Copy the project's metadata.json "
            "into your TRNS_HOME directory for full localisation support."
        )
        return {
            "default_language": "en",
            "token_capacity": 1000,
            "languages": {
                "en": {
                    "start_message": "Welcome! Please enter your authentication key.",
                    "invalid_key": "Invalid key. Please try again.",
                    "auth_success": "Authentication successful!",
                    "processing_video": "Processing video...",
                    "processing_complete": "✅ Processing complete.",
                    "error_occurred": "An error occurred:",
                    "not_authenticated": "You are not authenticated. Use /start to begin.",
                    "unknown_text": "Send a YouTube URL, Twitter/X.com URL, or video file to process."
                }
            }
        }
    except json.JSONDecodeError as e:
        logger.error(f"Error parsing metadata.json: {e}")
        raise


def save_metadata(metadata: Dict, metadata_path: str = "metadata.json"):
    """Save metadata to JSON file"""
    metadata_path = _resolve_path(metadata_path)
    try:
        _write_json_atomic(metadata_path, metadata)
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
    key_path = _resolve_path(key_path)
    try:
        with open(key_path, 'r', encoding='utf-8') as f:
            return f.read().strip()
    except FileNotFoundError:
        logger.error(f"Key file not found: {key_path} and AUTH_KEY environment variable not set")
        raise


def _load_user_state(user_id: int) -> Dict:
    """Load per-user runtime state from state/users/<telegram_id>.json."""
    try:
        return _load_json_file(_user_state_path(user_id), default={})
    except Exception as e:
        logger.error("Error loading state for user %s: %s", user_id, e)
        return {}


def _save_user_state(user_id: int, state: Dict):
    """Persist per-user runtime state."""
    _write_json_atomic(_user_state_path(user_id), state)


def is_user_authenticated(user_id: int, config_path: str = None) -> bool:
    """Check if a user is authenticated.

    Runtime authentication is stored in per-user state files. When an explicit
    config path is passed, or during migration from older installs, the legacy
    ``allowed_user_ids`` config field is still honored.
    """
    if _load_user_state(user_id).get("authenticated") is True:
        return True

    try:
        if config_path is None:
            config_path = os.getenv("CONFIG_PATH", "config.json")
        config_path = _resolve_path(config_path)
        if not os.path.exists(config_path):
            return False
        config = load_config(config_path)
        allowed_ids = config.get("allowed_user_ids", [])
        return user_id in allowed_ids
    except Exception as e:
        logger.error(f"Error checking authentication: {e}")
        return False


def _add_authenticated_user_to_config(user_id: int, config_path: str):
    """Legacy helper: add user ID to config.json with file locking."""
    config_path = _resolve_path(config_path)
    try:
        import fcntl

        has_fcntl = True
    except ImportError:
        has_fcntl = False

    lock_path = config_path + ".lock"

    try:
        lock_file = open(lock_path, "w")

        try:
            if has_fcntl:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            else:
                import time

                retry_count = 0
                while os.path.exists(lock_path + ".active") and retry_count < 50:
                    time.sleep(0.1)
                    retry_count += 1
                open(lock_path + ".active", "w").close()

            config = load_config(config_path)
            if "allowed_user_ids" not in config:
                config["allowed_user_ids"] = []

            if user_id in config["allowed_user_ids"]:
                logger.info(f"User {user_id} is already authenticated")
                return

            config["allowed_user_ids"].append(user_id)
            _write_json_atomic(config_path, config)
            logger.info(f"Added user {user_id} to allowed list in config.json")

        finally:
            if has_fcntl:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            else:
                if os.path.exists(lock_path + ".active"):
                    os.remove(lock_path + ".active")
            lock_file.close()

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


def add_authenticated_user(user_id: int, config_path: str = None):
    """
    Add a user to the authenticated set.

    New runtime state is written to ``state/users/<telegram_id>.json``. Passing
    an explicit config path preserves legacy behavior for migration tools and
    tests that still operate on ``allowed_user_ids``.
    """
    if config_path is not None:
        _add_authenticated_user_to_config(user_id, config_path)
        return

    with _settings_lock:
        state = _load_user_state(user_id)
        if state.get("authenticated") is True:
            logger.info(f"User {user_id} is already authenticated")
            return
        state["authenticated"] = True
        state.setdefault("settings", {})
        _save_user_state(user_id, state)
        logger.info(f"Added user {user_id} to per-user state")


def load_config(config_path: str = None) -> Dict:
    """Load configuration from JSON file.

    When ``config_path`` is omitted, uses ``CONFIG_PATH`` or ``config.json``,
    resolved under ``TRNS_HOME`` (same path rules as
    :func:`trns.transcription.main.load_config`).

    **Strict:** raises if the file is missing or JSON is invalid. For lenient
    loading that returns ``None``, use :func:`trns.transcription.main.load_config`.
    """
    if config_path is None:
        config_path = os.getenv("CONFIG_PATH", "config.json")
    config_path = _resolve_path(config_path)

    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        logger.error(f"Config file not found: {config_path}")
        raise
    except json.JSONDecodeError as e:
        logger.error(f"Error parsing config.json: {e}")
        raise


def get_timecode_default(config: Dict = None) -> bool:
    """Return the operator-level default for bot timecodes."""
    if config is not None:
        return bool(config.get("timecode", False))

    try:
        return bool(load_config().get("timecode", False))
    except Exception:
        return False


def save_config(config: Dict, config_path: str = "config.json"):
    """Save configuration to JSON file"""
    config_path = _resolve_path(config_path)
    try:
        with open(config_path, 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Error saving config.json: {e}")
        raise


def update_context(user_id: int, context_text: str, settings_path: str = None):
    """Update context for a specific user."""
    set_user_setting(user_id, "context", context_text, settings_path)


def reset_context(user_id: int, settings_path: str = None):
    """Reset context to empty string for a specific user"""
    update_context(user_id, "", settings_path)


def get_context(user_id: int, settings_path: str = None) -> str:
    """Get context for a specific user"""
    return get_user_setting(user_id, "context", default="", settings_path=settings_path)


def load_tokens(api_key_path: str = "api_key.txt") -> List[str]:
    """Load tokens from environment variable or file"""
    # Try environment variable first
    api_key = os.getenv("OPENROUTER_API_KEY")
    if api_key:
        return [api_key.strip()]

    # Fallback to file
    api_key_path = _resolve_path(api_key_path)
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
    api_key_path = _resolve_path(api_key_path)
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

    # Capacity is tracked in runtime state/global.json, not alongside tokens.


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


def _capacity_path(metadata_path: str = None) -> str:
    """Resolve capacity state path.

    Omitted/default runtime calls use state/global.json. Explicit paths keep the
    legacy metadata.json shape for tests and migration tooling.
    """
    if metadata_path is None:
        return _default_capacity_path()
    return _resolve_path(metadata_path)


def _load_capacity_state(path: str) -> Dict:
    """Load capacity state, tolerating old metadata.json-shaped files."""
    return _load_json_file(path, default={})


def get_daily_capacity(metadata_path: str = None) -> int:
    """
    Get current daily capacity, resetting to 1000 if it's a new day (UTC).
    Returns current capacity.
    """
    with _token_lock:
        capacity_path = _capacity_path(metadata_path)
        metadata = _load_capacity_state(capacity_path)

        # Get current UTC date
        today_utc = datetime.now(timezone.utc).date().isoformat()

        # Check if we have capacity tracking
        if "daily_capacity" not in metadata:
            metadata["daily_capacity"] = DAILY_CAPACITY
            metadata["last_capacity_date"] = today_utc
            _write_json_atomic(capacity_path, metadata)
            return DAILY_CAPACITY

        last_date = metadata.get("last_capacity_date")

        # If it's a new day, reset capacity
        if last_date != today_utc:
            metadata["daily_capacity"] = DAILY_CAPACITY
            metadata["last_capacity_date"] = today_utc
            _write_json_atomic(capacity_path, metadata)
            return DAILY_CAPACITY

        # Return current capacity
        return metadata.get("daily_capacity", DAILY_CAPACITY)


def decrement_daily_capacity(metadata_path: str = None) -> bool:
    """
    Decrement daily capacity by 1 (called for each LM API call).
    Returns True if capacity still available, False if exhausted.
    Automatically resets to 1000 if it's a new day (UTC).
    """
    with _token_lock:
        capacity_path = _capacity_path(metadata_path)
        metadata = _load_capacity_state(capacity_path)

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
        _write_json_atomic(capacity_path, metadata)

        return capacity >= 0


def check_capacity_at_start(metadata_path: str = None) -> Tuple[int, bool]:
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


def check_token_warning(metadata_path: str = None) -> bool:
    """Check if daily capacity is less than warning threshold (50)"""
    capacity = get_daily_capacity(metadata_path)
    return capacity < WARNING_THRESHOLD


def load_user_settings(settings_path: str = _DEFAULT_USER_SETTINGS_PATH) -> Dict:
    """
    Load user settings from JSON file.
    Creates file with empty dict if it doesn't exist.

    Returns:
        Dictionary mapping user_id (as string) to settings dict
    """
    settings_path = _resolve_path(settings_path)
    if not os.path.exists(settings_path):
        logger.info(f"User settings file not found, creating: {settings_path}")
        save_user_settings({}, settings_path)
        return {}

    try:
        with open(settings_path, 'r', encoding='utf-8') as f:
            settings = json.load(f)
            # Convert string keys to int for user_id access
            return {int(k) if k.isdigit() else k: v for k, v in settings.items()}
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as e:
        logger.error(f"Error parsing user_settings.json: {e}")
        return {}
    except Exception as e:
        logger.error(f"Error loading user settings: {e}")
        return {}


def save_user_settings(settings: Dict, settings_path: str = "user_settings.json"):
    """
    Save user settings to JSON file.

    Args:
        settings: Dictionary mapping user_id to settings dict
        settings_path: Path to settings file
    """
    settings_path = _resolve_path(settings_path)
    try:
        settings_for_json = {str(k): v for k, v in settings.items()}
        _write_json_atomic(settings_path, settings_for_json)
    except Exception as e:
        logger.error(f"Error saving user settings: {e}")
        raise


def _use_per_user_settings(settings_path: str = None) -> bool:
    """Return True when callers should use state/users/<id>.json."""
    return settings_path is None or settings_path == _DEFAULT_USER_SETTINGS_PATH


def get_user_setting(user_id: int, setting_key: str, default=None, settings_path: str = None):
    """
    Get a specific setting for a user.

    Args:
        user_id: Telegram user ID
        setting_key: Setting key to retrieve
        default: Default value if setting not found
        settings_path: Path to settings file

    Returns:
        Setting value or default
    """
    if _use_per_user_settings(settings_path):
        state = _load_user_state(user_id)
        settings = state.get("settings", {})
        if not isinstance(settings, dict):
            return default
        return settings.get(setting_key, default)

    settings = load_user_settings(settings_path)

    if user_id not in settings:
        return default

    return settings[user_id].get(setting_key, default)


def set_user_setting(user_id: int, setting_key: str, value, settings_path: str = None):
    """
    Set a specific setting for a user.
    Uses a lock to prevent concurrent read-modify-write races.

    Args:
        user_id: Telegram user ID
        setting_key: Setting key to set
        value: Value to set
        settings_path: Path to settings file
    """
    with _settings_lock:
        if _use_per_user_settings(settings_path):
            state = _load_user_state(user_id)
            settings = state.get("settings", {})
            if not isinstance(settings, dict):
                settings = {}
            settings[setting_key] = value
            state["settings"] = settings
            _save_user_state(user_id, state)
            return

        settings = load_user_settings(settings_path)

        if user_id not in settings:
            settings[user_id] = {}

        settings[user_id][setting_key] = value
        save_user_settings(settings, settings_path)


def initialize_user_settings(user_id: int, settings_path: str = None):
    """
    Initialize default settings for a new user.
    Default: show_original_translation=True, show_transcription=True.
    Timecodes intentionally stay config-backed until a user toggles them.

    Args:
        user_id: Telegram user ID
        settings_path: Path to settings file
    """
    with _settings_lock:
        defaults = {
            "show_original_translation": True,
            "show_transcription": True,
        }

        if _use_per_user_settings(settings_path):
            state = _load_user_state(user_id)
            settings = state.get("settings", {})
            if not isinstance(settings, dict):
                settings = {}
            changed = False
            for key, value in defaults.items():
                if key not in settings:
                    settings[key] = value
                    changed = True
            if changed:
                state["settings"] = settings
                _save_user_state(user_id, state)
            logger.info(f"Initialized default settings for user {user_id}")
            return

        settings = load_user_settings(settings_path)

        if user_id not in settings:
            settings[user_id] = defaults
            save_user_settings(settings, settings_path)
        logger.info(f"Initialized default settings for user {user_id}")
