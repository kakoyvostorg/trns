# Update — March 19, 2026

## Summary

Code review and bug-fix pass across the entire codebase. **10 files changed, 482 insertions, 759 deletions** — net reduction of ~280 lines while fixing 9 bugs and improving architecture.

## Bugs Fixed

### 1. Thread-safety: `builtins.print` monkey-patching (Critical)
**File:** `routes.py`, `pipeline.py`
The Telegram bot captured pipeline output by replacing the global `builtins.print` function. With two concurrent users, their outputs would cross-contaminate. Replaced with a per-pipeline `output_callback` parameter — no global state mutation.

### 2. Overlap removal corrupting translated text
**File:** `pipeline.py`
When removing repeated text between chunks, the code sliced translated text by the character count of the *original* language overlap. Since translations have different lengths, this produced garbled output. Fixed by matching words independently for each language.

### 3. Off-by-one in daily capacity tracking
**File:** `utils.py`
`decrement_daily_capacity` returned `capacity > 0` after decrementing. When capacity was exactly 1, it decremented to 0 and returned `False` — consuming the token but reporting failure. Fixed to `capacity >= 0`.

### 4. Token staleness in LM client
**File:** `language_model.py`
After token rotation (multi-key setup in `api_key.txt`), the OpenAI client kept using the old API key. Added a check: if `self.client.api_key != token`, reinitialize the client.

### 5. `_load_prompt()` → `_load_prompts()` typo
**File:** `pipeline.py` (line 532)
Called a non-existent method name after language detection. Fixed to match the actual method.

### 6. Missing user settings for YouTube videos
**File:** `routes.py`
`show_original_translation` and `show_transcription` user preferences were loaded for Twitter URLs but not YouTube. Extracted shared `_process_url_video()` that handles both.

### 7. Temp file leak on shutdown
**File:** `whisper_transcriber.py`
When shutdown was triggered during audio extraction, temp files (`.wav`, `.mp4`, etc.) were left on disk. Added `_cleanup_temp_files()` called at all 4 early-return points.

### 8. Inline `type('obj', ...)` hacks in webhook handler
**File:** `server.py`
Bot API message wrappers were defined as anonymous classes inside the request handler — recreated on every webhook call. Extracted to proper module-level classes (`BotAPIMessage`, `_ChatInfo`, `_UserInfo`, `_FileInfo`, `SimpleUpdate`).

### 9. Bot file paths not resolved against TRNS_HOME
**File:** `routes.py`, `language_model.py`
The bot created `LMProcessor` with relative paths like `"api_key.txt"` — these resolved against the process CWD, not the project directory. Added `_resolve_args_paths()` that resolves all resource paths against `TRNS_HOME` (or CWD), matching CLI behavior. Also passed `metadata_path` through to `LMProcessor`.

## Code Quality Improvements

- **~400 lines of duplication eliminated** in `routes.py`: extracted `_run_pipeline_with_output()` and `_process_url_video()` shared helpers
- **Thread-safe user state access**: added `_user_states_lock` with `get_user_state()`/`set_user_state()` wrappers
- **Redundant client reinit removed** from `language_model.py`: two identical try/except blocks for OpenAI client refresh replaced by centralized logic in `_get_token_and_decrement()`
- **All 117 tests pass**
