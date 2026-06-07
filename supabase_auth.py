"""
Authentication backed by Supabase Auth (GoTrue).

The app stays server-rendered and keeps its own Flask session, but credentials,
the user list and password-reset/invite e-mails are handled by Supabase. This
fixes e-mail (Supabase sends it, so it is not affected by the Hugging Face SMTP
block) and lets you manage users in the Supabase dashboard.

Config (env / Space):
    SUPABASE_URL        -> project url (reused from user_store)
    SUPABASE_KEY        -> service_role key (admin ops; reused from user_store)
    SUPABASE_ANON_KEY   -> anon public key (sign-in / recover / client reset)

GoTrue endpoints used:
    POST /auth/v1/token?grant_type=password   (sign in, anon key)
    POST /auth/v1/recover                      (send reset e-mail, anon key)
    POST /auth/v1/invite                       (admin invite e-mail, service key)
    GET/POST/PUT/DELETE /auth/v1/admin/users   (admin user management, service key)
"""

import os

import user_store  # reuse SUPABASE_URL / SUPABASE_KEY

ANON_KEY = (
    os.environ.get("SUPABASE_ANON_KEY", "").strip()
    or os.environ.get("SUPABASE_ANON", "").strip()
)


def _url():
    return user_store.SUPABASE_URL


def _service_key():
    return user_store.SUPABASE_KEY


def enabled():
    return bool(_url() and _service_key() and ANON_KEY)


def _anon_headers(extra=None):
    h = {"apikey": ANON_KEY, "Content-Type": "application/json"}
    if extra:
        h.update(extra)
    return h


def _service_headers(extra=None):
    h = {
        "apikey": _service_key(),
        "Authorization": f"Bearer {_service_key()}",
        "Content-Type": "application/json",
    }
    if extra:
        h.update(extra)
    return h


# --- User-facing -----------------------------------------------------------

def sign_in(email, password):
    """Return the user dict on success, None on bad credentials."""
    import requests

    resp = requests.post(
        f"{_url()}/auth/v1/token",
        headers=_anon_headers(),
        params={"grant_type": "password"},
        json={"email": email, "password": password},
        timeout=20,
    )
    if resp.status_code != 200:
        return None
    data = resp.json()
    return data.get("user")


def send_recovery(email, redirect_to=None):
    """Ask Supabase to e-mail a password reset link."""
    import requests

    params = {"redirect_to": redirect_to} if redirect_to else None
    resp = requests.post(
        f"{_url()}/auth/v1/recover",
        headers=_anon_headers(),
        params=params,
        json={"email": email},
        timeout=20,
    )
    # GoTrue always returns 200 to avoid leaking which e-mails exist.
    return resp.status_code in (200, 204)


# --- Admin -----------------------------------------------------------------

def admin_list_users():
    import requests

    resp = requests.get(
        f"{_url()}/auth/v1/admin/users",
        headers=_service_headers(),
        params={"per_page": "200"},
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, dict):
        return data.get("users", [])
    return data or []


def admin_get_by_email(email):
    email = (email or "").lower()
    for u in admin_list_users():
        if (u.get("email") or "").lower() == email:
            return u
    return None


def admin_create_user(email, password, name=None, email_confirm=True):
    import requests

    body = {
        "email": email,
        "password": password,
        "email_confirm": email_confirm,
    }
    if name:
        body["user_metadata"] = {"name": name}
    resp = requests.post(
        f"{_url()}/auth/v1/admin/users",
        headers=_service_headers(),
        json=body,
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json()


def ensure_user(email, password, name=None):
    """Create the user if missing (used to bootstrap the superadmin)."""
    if admin_get_by_email(email):
        return False
    admin_create_user(email, password, name=name, email_confirm=True)
    return True


def admin_invite(email, redirect_to=None):
    import requests

    params = {"redirect_to": redirect_to} if redirect_to else None
    resp = requests.post(
        f"{_url()}/auth/v1/invite",
        headers=_service_headers(),
        params=params,
        json={"email": email},
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json()


def admin_set_password(user_id, password):
    import requests

    resp = requests.put(
        f"{_url()}/auth/v1/admin/users/{user_id}",
        headers=_service_headers(),
        json={"password": password},
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json()


def admin_delete_user(user_id):
    import requests

    resp = requests.delete(
        f"{_url()}/auth/v1/admin/users/{user_id}",
        headers=_service_headers(),
        timeout=20,
    )
    return resp.status_code in (200, 204)
