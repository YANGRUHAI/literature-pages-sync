#!/usr/bin/env python3
"""Incrementally sync read-only interactive literature web apps from Google Drive (v2).

Hardened for 100,000+ papers & zero-maintenance operation:
- Full Google Drive API v3 pagination (pageSize=1000 + nextPageToken).
- Syncs strictly runtime files (HTML shell -> index.html + shards_*/*.js; skips shard_manifest.json).
- Verifies shard completeness against SHELL.cfg (totalDocs/metaCap/sliceCap) to prevent mid-upload tearing.
- 5-minute Cache-Control with ETag revalidation on *.js so unsealed tail shards never serve stale data.
- Suppresses redundant GitHub cache writes on no-op runs (refreshes every 4 days before 7-day eviction).
"""

from __future__ import annotations

import base64
import json
import math
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
CACHE_REFRESH_SECONDS = 4 * 86400  # Refresh cache every 4 days to beat GitHub's 7-day eviction

SITES = [
    {
        "key": "me",
        "title": "Literature on Middle East Studies",
        "project_name": "literature-on-middle-east-studies",
        "topic_folder_id": "1to2zAmb3zlPWHYNMvfFXzMm4vxv1Eh-k",
        "html_filename": "middle_east_literature_tag_web.html",
        "shards_folder_id": "19mKtSIECDc_t-XYfTflHgT0Uy-_Tafnt",
        "shards_dir_name": "shards_me",
        "dist_dir": Path("dist/middle_east"),
    },
    {
        "key": "autism",
        "title": "Literature on Autism Research",
        "project_name": "literature-on-autism-research",
        "topic_folder_id": "1V1dsevYYd0-CxHfaWo-Fhig3vj1fDuPM",
        "html_filename": "autism_literature_tag_web.html",
        "shards_folder_id": "1KZFYwXcr3eW-H0E52C3NTnv79PPmx-v9",
        "shards_dir_name": "shards_autism",
        "dist_dir": Path("dist/autism"),
    },
]

STATE_FILE = Path(".sync_state/state.json")


def load_credentials() -> service_account.Credentials:
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if not raw:
        raise RuntimeError("Missing GOOGLE_SERVICE_ACCOUNT_JSON environment variable.")
    if not raw.startswith("{"):
        raw = base64.b64decode(raw).decode("utf-8")
    info = json.loads(raw)
    return service_account.Credentials.from_service_account_info(info, scopes=SCOPES)


def list_folder_files(service: Any, folder_id: str) -> list[dict[str, Any]]:
    """List all non-trashed files in a Drive folder with full pagination."""
    files: list[dict[str, Any]] = []
    page_token: str | None = None
    query = f"'{folder_id}' in parents and trashed = false"
    while True:
        resp = (
            service.files()
            .list(
                q=query,
                pageSize=1000,
                fields="nextPageToken, files(id, name, md5Checksum, modifiedTime, size, mimeType)",
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
                pageToken=page_token,
            )
            .execute()
        )
        files.extend(resp.get("files", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return files


def pick_latest_by_name(files: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Deduplicate by filename, keeping the newest modifiedTime."""
    latest: dict[str, dict[str, Any]] = {}
    for f in files:
        if f.get("mimeType") == "application/vnd.google-apps.folder":
            continue
        name = f["name"]
        prev = latest.get(name)
        if prev is None or f.get("modifiedTime", "") > prev.get("modifiedTime", ""):
            latest[name] = f
    return latest


def download_file(service: Any, file_id: str, dest: Path, max_retries: int = 3) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp_dest = dest.with_suffix(dest.suffix + ".part")
    for attempt in range(1, max_retries + 1):
        try:
            request = service.files().get_media(fileId=file_id, supportsAllDrives=True)
            with tmp_dest.open("wb") as fh:
                downloader = MediaIoBaseDownload(fh, request, chunksize=8 * 1024 * 1024)
                done = False
                while not done:
                    _, done = downloader.next_chunk()
            tmp_dest.replace(dest)
            return
        except Exception as exc:
            if tmp_dest.exists():
                tmp_dest.unlink()
            if attempt == max_retries:
                raise RuntimeError(f"Failed downloading {file_id} -> {dest}: {exc}") from exc
            time.sleep(2**attempt)


def file_signature(meta: dict[str, Any]) -> str:
    return f"{meta['id']}:{meta.get('md5Checksum', '')}:{meta.get('modifiedTime', '')}"


def write_cloudflare_headers(dist_dir: Path, shards_dir_name: str) -> None:
    # Both index.html and *.js use 5-min revalidation so mutable tail shards never go stale for 24h
    headers_content = (
        "/*\n"
        "  X-Content-Type-Options: nosniff\n"
        "  Referrer-Policy: strict-origin-when-cross-origin\n"
        "  X-Frame-Options: SAMEORIGIN\n"
        "\n"
        "/index.html\n"
        "  Cache-Control: public, max-age=300, must-revalidate\n"
        "\n"
        f"/{shards_dir_name}/*.js\n"
        "  Cache-Control: public, max-age=300, must-revalidate\n"
    )
    (dist_dir / "_headers").write_text(headers_content, encoding="utf-8")


def verify_shard_completeness(index_path: Path, shards_dir: Path) -> list[str]:
    """Verify all expected meta and slice shards required by SHELL.cfg exist and are non-empty."""
    html_text = index_path.read_text(encoding="utf-8")
    m = re.search(r'const SHELL\s*=\s*(\{"cfg":\s*\{.*?\})', html_text)
    if not m:
        m_cfg = re.search(r'"cfg"\s*:\s*(\{[^}]+\})', html_text)
        if not m_cfg:
            return []
        cfg = json.loads(m_cfg.group(1))
    else:
        m_cfg = re.search(r'"cfg"\s*:\s*(\{[^}]+\})', m.group(1))
        cfg = json.loads(m_cfg.group(1)) if m_cfg else {}

    total_docs = int(cfg.get("totalDocs", 0))
    meta_cap = int(cfg.get("metaCap", 2000))
    slice_cap = int(cfg.get("sliceCap", 200))
    prefix = str(cfg.get("prefix", ""))
    if not total_docs or not prefix:
        return []

    expected_meta = math.ceil(total_docs / meta_cap)
    expected_slice = math.ceil(total_docs / slice_cap)
    missing: list[str] = []

    for i in range(1, expected_meta + 1):
        fname = f"{prefix}_meta_{str(i).zfill(3)}.js"
        fpath = shards_dir / fname
        if not fpath.exists() or fpath.stat().st_size == 0:
            missing.append(fname)
    for i in range(1, expected_slice + 1):
        fname = f"{prefix}_shard_{str(i).zfill(3)}.js"
        fpath = shards_dir / fname
        if not fpath.exists() or fpath.stat().st_size == 0:
            missing.append(fname)
    return missing


def sync_site(service: Any, site: dict[str, Any], prev_state: dict[str, str]) -> tuple[bool, dict[str, str]]:
    print(f"\n=== Syncing {site['title']} ({site['project_name']}) ===")
    dist_dir: Path = site["dist_dir"]
    shards_dir = dist_dir / site["shards_dir_name"]
    dist_dir.mkdir(parents=True, exist_ok=True)
    shards_dir.mkdir(parents=True, exist_ok=True)

    topic_files = pick_latest_by_name(list_folder_files(service, site["topic_folder_id"]))
    html_meta = topic_files.get(site["html_filename"])
    if not html_meta:
        raise RuntimeError(
            f"Main HTML file '{site['html_filename']}' not found in folder {site['topic_folder_id']}."
        )

    # Strictly sync runtime .js shards (skip shard_manifest.json which the browser never fetches)
    shard_files_all = pick_latest_by_name(list_folder_files(service, site["shards_folder_id"]))
    shard_files = {name: meta for name, meta in shard_files_all.items() if name.endswith(".js")}

    desired: dict[str, dict[str, Any]] = {"index.html": html_meta}
    for name, meta in shard_files.items():
        desired[f"{site['shards_dir_name']}/{name}"] = meta

    new_state: dict[str, str] = {}
    downloaded = 0
    skipped = 0
    changed = False

    for rel_path, meta in sorted(desired.items()):
        sig = file_signature(meta)
        new_state[rel_path] = sig
        dest_path = dist_dir / rel_path
        if prev_state.get(rel_path) == sig and dest_path.exists() and dest_path.stat().st_size > 0:
            skipped += 1
            continue
        print(f"  [DOWN] {rel_path}")
        download_file(service, meta["id"], dest_path)
        downloaded += 1
        changed = True

    expected_shard_names = set(shard_files.keys())
    removed = 0
    for existing in shards_dir.iterdir():
        if existing.is_file() and existing.name not in expected_shard_names:
            print(f"  [DEL]  {site['shards_dir_name']}/{existing.name}")
            existing.unlink()
            removed += 1
            changed = True

    index_path = dist_dir / "index.html"
    if not index_path.exists() or index_path.stat().st_size < 10_000:
        raise RuntimeError(f"Sanity check failed: {index_path} is missing or unexpectedly small.")

    missing_shards = verify_shard_completeness(index_path, shards_dir)
    if missing_shards:
        print(
            f"  [WARN] Upstream build appears mid-upload; missing shards: {missing_shards}. "
            "Deferring deployment of this site to next cycle."
        )
        return False, prev_state

    write_cloudflare_headers(dist_dir, site["shards_dir_name"])
    print(
        f"  Summary: total={len(desired)}, downloaded={downloaded}, "
        f"cached={skipped}, removed={removed}, changed={changed}"
    )
    return changed, new_state


def write_github_output(outputs: dict[str, str]) -> None:
    gh_out = os.environ.get("GITHUB_OUTPUT")
    if not gh_out:
        for k, v in outputs.items():
            print(f"OUTPUT {k}={v}")
        return
    with open(gh_out, "a", encoding="utf-8") as fh:
        for k, v in outputs.items():
            fh.write(f"{k}={v}\n")


def main() -> int:
    force_deploy = os.environ.get("FORCE_DEPLOY", "false").lower() == "true"
    creds = load_credentials()
    service = build("drive", "v3", credentials=creds, cache_discovery=False)

    raw_state: dict[str, Any] = {}
    if STATE_FILE.exists():
        try:
            raw_state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            raw_state = {}

    last_cache_saved = float(raw_state.get("_meta", {}).get("cache_saved_epoch", 0.0))
    now = time.time()

    all_new_state: dict[str, Any] = {}
    outputs: dict[str, str] = {}
    any_changed = False

    for site in SITES:
        key = site["key"]
        prev_site_state = raw_state.get(key, {})
        changed, new_site_state = sync_site(service, site, prev_site_state)
        all_new_state[key] = new_site_state
        if changed:
            any_changed = True
        should_deploy = (changed or force_deploy) and bool(new_site_state)
        outputs[f"deploy_{key}"] = "true" if should_deploy else "false"

    cache_stale = (now - last_cache_saved) > CACHE_REFRESH_SECONDS
    save_cache = any_changed or force_deploy or cache_stale
    outputs["save_cache"] = "true" if save_cache else "false"

    all_new_state["_meta"] = {
        "cache_saved_epoch": now if save_cache else last_cache_saved,
    }

    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(all_new_state, indent=2, sort_keys=True), encoding="utf-8")
    write_github_output(outputs)
    return 0


if __name__ == "__main__":
    sys.exit(main())
