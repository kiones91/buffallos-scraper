"""
Persistent user/allowlist storage.

Hugging Face Spaces (free tier) has an *ephemeral* filesystem: anything written
to disk is wiped on every restart/rebuild. To persist user accounts at zero
cost we store a single JSON file inside a (private) Hugging Face *Dataset* repo
and read/write it through the Hub API.

Configuration (environment variables / Space secrets):
    USERS_REPO  -> dataset repo id, e.g. "Kiones/buffallos-scraper-users"
    HF_TOKEN    -> a Hugging Face token with WRITE access (set as a Space secret)
    USERS_FILE  -> local fallback path (used only when the Hub is not configured)

If USERS_REPO/HF_TOKEN are not set, it falls back to a local JSON file. That is
fine for local development, but on a free HF Space it will NOT persist across
restarts -- so configure the dataset for production.

State schema:
    {
      "users": {
        "email@x.com": {
          "password_hash": "...",
          "role": "admin" | "user",
          "active": true,
          "pending_reset": false,
          "created_at": 1700000000.0,
          "updated_at": 1700000000.0
        }
      },
      "allowlist": ["email@x.com", ...]
    }
"""

import io
import json
import os
import threading
import time

FILENAME = "users.json"

USERS_REPO = os.environ.get("USERS_REPO", "").strip()
HF_TOKEN = (
    os.environ.get("HF_TOKEN", "").strip()
    or os.environ.get("HF_API_TOKEN", "").strip()
    or os.environ.get("HUGGINGFACE_TOKEN", "").strip()
)
LOCAL_PATH = os.environ.get("USERS_FILE", "data/users.json")

_LOCK = threading.RLock()
_STATE = None
_LOADED = False


def use_hub():
    return bool(USERS_REPO and HF_TOKEN)


def _default_state():
    return {"users": {}, "allowlist": []}


def _load_from_hub():
    from huggingface_hub import hf_hub_download

    try:
        path = hf_hub_download(
            repo_id=USERS_REPO,
            filename=FILENAME,
            repo_type="dataset",
            token=HF_TOKEN,
            force_download=True,
        )
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception as exc:
        # File or repo not there yet -> start empty. Any other error: log + empty.
        print(f"[user_store] starting empty (could not load from hub: {exc})")
        return _default_state()


def _save_to_hub(state):
    from huggingface_hub import HfApi

    api = HfApi(token=HF_TOKEN)
    data = json.dumps(state, ensure_ascii=False, indent=2).encode("utf-8")
    api.upload_file(
        path_or_fileobj=io.BytesIO(data),
        path_in_repo=FILENAME,
        repo_id=USERS_REPO,
        repo_type="dataset",
        commit_message="update users",
    )


def _load_local():
    try:
        with open(LOCAL_PATH, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return _default_state()


def _save_local(state):
    folder = os.path.dirname(LOCAL_PATH)
    if folder:
        os.makedirs(folder, exist_ok=True)
    with open(LOCAL_PATH, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=2)


def _ensure_loaded():
    global _STATE, _LOADED
    if _LOADED:
        return
    with _LOCK:
        if _LOADED:
            return
        _STATE = _load_from_hub() if use_hub() else _load_local()
        _STATE.setdefault("users", {})
        _STATE.setdefault("allowlist", [])
        _LOADED = True


def _persist():
    if use_hub():
        _save_to_hub(_STATE)
    else:
        _save_local(_STATE)


# --- Public API ----------------------------------------------------------

def get_user(email):
    _ensure_loaded()
    with _LOCK:
        return _STATE["users"].get(email)


def list_users():
    _ensure_loaded()
    with _LOCK:
        return {e: dict(v) for e, v in _STATE["users"].items()}


def get_allowlist():
    _ensure_loaded()
    with _LOCK:
        return list(_STATE["allowlist"])


def is_allowed(email):
    _ensure_loaded()
    with _LOCK:
        return email in _STATE["allowlist"]


def add_allowed(email):
    _ensure_loaded()
    with _LOCK:
        if email not in _STATE["allowlist"]:
            _STATE["allowlist"].append(email)
            _persist()


def remove_allowed(email):
    _ensure_loaded()
    with _LOCK:
        if email in _STATE["allowlist"]:
            _STATE["allowlist"].remove(email)
            _persist()


def upsert_user(email, password_hash, role="user"):
    _ensure_loaded()
    now = time.time()
    with _LOCK:
        existing = _STATE["users"].get(email, {})
        existing.update(
            {
                "password_hash": password_hash,
                "role": role or existing.get("role", "user"),
                "active": True,
                "pending_reset": False,
                "updated_at": now,
            }
        )
        existing.setdefault("created_at", now)
        _STATE["users"][email] = existing
        _persist()
        return dict(existing)


def set_password(email, password_hash):
    _ensure_loaded()
    with _LOCK:
        user = _STATE["users"].get(email)
        if not user:
            return False
        user["password_hash"] = password_hash
        user["pending_reset"] = False
        user["updated_at"] = time.time()
        _persist()
        return True


def set_pending_reset(email, value=True):
    _ensure_loaded()
    with _LOCK:
        user = _STATE["users"].get(email)
        if not user:
            return False
        user["pending_reset"] = bool(value)
        user["updated_at"] = time.time()
        _persist()
        return True


def set_active(email, active):
    _ensure_loaded()
    with _LOCK:
        user = _STATE["users"].get(email)
        if not user:
            return False
        user["active"] = bool(active)
        user["updated_at"] = time.time()
        _persist()
        return True


def delete_user(email):
    _ensure_loaded()
    with _LOCK:
        existed = _STATE["users"].pop(email, None) is not None
        if existed:
            _persist()
        return existed
