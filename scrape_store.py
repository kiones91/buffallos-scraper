"""
Per-user library of scraped sites, stored in Supabase.

- The ZIP file goes to Supabase **Storage** (object storage), bucket `scrapes`.
- The metadata (owner, url, name, size, date) goes to a Postgres table `scrapes`
  accessed via PostgREST.

Reuses the Supabase credentials configured in user_store (SUPABASE_URL +
SUPABASE_KEY, where the key must be the service_role secret).

Required one-time setup in Supabase (SQL editor):

    create table if not exists scrapes (
      id uuid primary key default gen_random_uuid(),
      user_email text not null,
      url text not null,
      site_name text not null,
      storage_path text not null,
      size_bytes bigint default 0,
      created_at timestamptz default now()
    );
    create index if not exists scrapes_user_idx
      on scrapes (user_email, created_at desc);

The storage bucket is created automatically by ensure_bucket().
"""

import os
import re
import uuid

import user_store  # reuse SUPABASE_URL / SUPABASE_KEY

BUCKET = os.environ.get("SUPABASE_BUCKET", "scrapes").strip()


def enabled():
    return user_store.use_supabase()


def _base():
    return user_store.SUPABASE_URL


def _key():
    return user_store.SUPABASE_KEY


def _headers(extra=None):
    h = {
        "apikey": _key(),
        "Authorization": f"Bearer {_key()}",
    }
    if extra:
        h.update(extra)
    return h


def _sanitize(email):
    return re.sub(r"[^a-zA-Z0-9_.-]", "_", (email or "anon").lower())


# --- Storage -------------------------------------------------------------

def ensure_bucket():
    """Create the private bucket if it does not exist (idempotent)."""
    import requests

    try:
        resp = requests.post(
            f"{_base()}/storage/v1/bucket",
            headers=_headers({"Content-Type": "application/json"}),
            json={"id": BUCKET, "name": BUCKET, "public": False},
            timeout=15,
        )
        # 200 created; 400/409 -> already exists. Anything else: surface later.
        if resp.status_code not in (200, 400, 409):
            print(f"[scrape_store] ensure_bucket status {resp.status_code}: {resp.text[:200]}")
    except Exception as exc:
        print(f"[scrape_store] ensure_bucket failed: {exc}")


def upload_file(local_path, storage_path, content_type="application/zip"):
    import requests

    with open(local_path, "rb") as fh:
        data = fh.read()
    resp = requests.post(
        f"{_base()}/storage/v1/object/{BUCKET}/{storage_path}",
        headers=_headers({"Content-Type": content_type, "x-upsert": "true"}),
        data=data,
        timeout=120,
    )
    resp.raise_for_status()
    return len(data)


def download_bytes(storage_path):
    import requests

    resp = requests.get(
        f"{_base()}/storage/v1/object/{BUCKET}/{storage_path}",
        headers=_headers(),
        timeout=120,
    )
    resp.raise_for_status()
    return resp.content


def delete_object(storage_path):
    import requests

    try:
        requests.delete(
            f"{_base()}/storage/v1/object/{BUCKET}/{storage_path}",
            headers=_headers(),
            timeout=30,
        )
    except Exception as exc:
        print(f"[scrape_store] delete_object failed: {exc}")


# --- Metadata table ------------------------------------------------------

def add_scrape(user_email, url, site_name, local_zip_path):
    """Upload the zip and record metadata. Returns the inserted row (dict)."""
    import requests

    ensure_bucket()
    scrape_id = str(uuid.uuid4())
    storage_path = f"{_sanitize(user_email)}/{scrape_id}.zip"
    size = upload_file(local_zip_path, storage_path)

    row = {
        "id": scrape_id,
        "user_email": user_email,
        "url": url,
        "site_name": site_name,
        "storage_path": storage_path,
        "size_bytes": size,
    }
    resp = requests.post(
        f"{_base()}/rest/v1/scrapes",
        headers=_headers({
            "Content-Type": "application/json",
            "Prefer": "return=representation",
        }),
        json=row,
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    return data[0] if isinstance(data, list) and data else row


def list_scrapes(user_email):
    import requests

    resp = requests.get(
        f"{_base()}/rest/v1/scrapes",
        headers=_headers(),
        params={
            "user_email": f"eq.{user_email}",
            "select": "id,url,site_name,size_bytes,created_at",
            "order": "created_at.desc",
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def get_scrape(scrape_id, user_email):
    import requests

    resp = requests.get(
        f"{_base()}/rest/v1/scrapes",
        headers=_headers(),
        params={
            "id": f"eq.{scrape_id}",
            "user_email": f"eq.{user_email}",
            "select": "*",
        },
        timeout=30,
    )
    resp.raise_for_status()
    rows = resp.json()
    return rows[0] if rows else None


def delete_scrape(scrape_id, user_email):
    import requests

    row = get_scrape(scrape_id, user_email)
    if not row:
        return False
    delete_object(row["storage_path"])
    requests.delete(
        f"{_base()}/rest/v1/scrapes",
        headers=_headers(),
        params={"id": f"eq.{scrape_id}", "user_email": f"eq.{user_email}"},
        timeout=30,
    )
    return True
