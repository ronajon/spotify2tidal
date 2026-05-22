"""
Spotify Playlist Scraper → Tidal Importer
==========================================
Uses SpotAPI (no Selenium, no API key) to fetch all tracks from a public
Spotify playlist, then imports only NEW tracks into an existing Tidal playlist.

Requirements:
    pip install spotapi tidalapi pymongo redis websockets python-dotenv

.env file:
    TELEGRAM_TOKEN=your_bot_token
    TELEGRAM_ID=your_chat_id

Usage:
    python spotify_to_tidal.py <spotify_playlist_id> <tidal_playlist_id> [--dry-run]
                               [--logtoscreen | --logtofile | --lognone]
"""

import argparse
import json
import logging
import os
import pathlib
import sys
import time
import urllib.error
import urllib.request

import tidalapi
from dotenv import load_dotenv
from spotapi import PublicPlaylist

# ── CONFIG ────────────────────────────────────────────────────────────────────

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_ID    = os.getenv("TELEGRAM_ID", "")
SESSION_FILE   = pathlib.Path("tidal_session.json")
LOG_FILE       = pathlib.Path("spotify_to_tidal.log")

# ── ARGS ──────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Import a Spotify playlist into Tidal (new tracks only)."
    )
    parser.add_argument(
        "spotify_playlist_id",
        help="Spotify playlist ID (e.g. 1HDm8vAAGZjLECGzsjdMU7)",
    )
    parser.add_argument(
        "tidal_playlist_id",
        help="Tidal playlist UUID (e.g. ea35a54e-2a76-4f7d-b942-e2e4e228fd48)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview which tracks would be added without modifying Tidal.",
    )

    log_group = parser.add_mutually_exclusive_group()
    log_group.add_argument(
        "--logtoscreen",
        action="store_true",
        default=True,
        help="Print log output to the screen (default).",
    )
    log_group.add_argument(
        "--logtofile",
        action="store_true",
        help=f"Write log output to {LOG_FILE}.",
    )
    log_group.add_argument(
        "--lognone",
        action="store_true",
        help="Suppress all log output.",
    )
    return parser.parse_args()

# ── LOGGING ───────────────────────────────────────────────────────────────────

logger = logging.getLogger("spotify_to_tidal")

def setup_logging(args: argparse.Namespace) -> None:
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(message)s")

    if args.lognone:
        logger.addHandler(logging.NullHandler())
    elif args.logtofile:
        handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
        handler.setFormatter(fmt)
        logger.addHandler(handler)
    else:  # --logtoscreen (default)
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(fmt)
        logger.addHandler(handler)

def log(msg: str) -> None:
    logger.info(msg)

# ── SPOTIFY ───────────────────────────────────────────────────────────────────

def scrape_spotify(playlist_id: str) -> list[dict]:
    log(f"🔍 Fetching Spotify playlist {playlist_id}...")
    playlist = PublicPlaylist(playlist_id)

    tracks = []
    seen   = set()

    for batch in playlist.paginate_playlist():
        for item in batch.get("items", []):
            try:
                data = item.get("itemV2", {}).get("data", {})

                if data.get("__typename") != "Track":
                    continue

                title = data.get("name", "").strip()

                artists = ", ".join(
                    a["profile"]["name"]
                    for a in data.get("artists", {}).get("items", [])
                    if a.get("profile", {}).get("name")
                )

                album = data.get("albumOfTrack", {}).get("name", "").strip()
                year  = str(
                    data.get("albumOfTrack", {}).get("date", {}).get("year", "")
                )

                key = (title.lower(), artists.lower())
                if key in seen or not title:
                    continue
                seen.add(key)

                tracks.append({"title": title, "artist": artists, "album": album, "year": year})

            except Exception:
                continue

        log(f"  fetched {len(tracks)} tracks so far...")

    log(f"✅ Scraped {len(tracks)} tracks total.")
    return tracks

# ── TIDAL SESSION ─────────────────────────────────────────────────────────────

def load_or_create_session() -> tidalapi.Session:
    session = tidalapi.Session()

    if SESSION_FILE.exists():
        data = json.loads(SESSION_FILE.read_text())
        try:
            session.load_oauth_session(
                data["token_type"],
                data["access_token"],
                data["refresh_token"],
                data.get("expiry_time"),
            )
            if session.check_login():
                log("[tidal] Session restored ✓")
                return session
            log("[tidal] Saved session expired, re-authenticating…")
        except Exception as e:
            log(f"[tidal] Could not restore session ({e}), re-authenticating…")

    login, future = session.login_oauth()
    # Always print the login URL to screen regardless of log mode
    print(f"[tidal] Open this URL in your browser to log in:\n  {login.verification_uri_complete}")
    future.result()

    SESSION_FILE.write_text(json.dumps({
        "token_type":    session.token_type,
        "access_token":  session.access_token,
        "refresh_token": session.refresh_token,
        "expiry_time":   session.expiry_time.isoformat() if session.expiry_time else None,
    }))
    log(f"[tidal] Session saved to {SESSION_FILE}")
    return session

# ── TIDAL IMPORT ──────────────────────────────────────────────────────────────

def get_existing_tidal_ids(playlist: tidalapi.Playlist) -> set[int]:
    log("📋 Fetching existing tracks from Tidal playlist...")
    ids = {track.id for track in playlist.tracks()}
    log(f"   {len(ids)} tracks already in playlist.")
    return ids


def find_tidal_track(session: tidalapi.Session, title: str, artist: str):
    query = f"{title} {artist}"
    try:
        results = session.search(query, models=[tidalapi.Track])
        for track in results.get("tracks", []):
            t_title  = track.name.lower()
            t_artist = track.artist.name.lower() if track.artist else ""
            if title.lower() in t_title or t_title in title.lower():
                if any(word in t_artist for word in artist.lower().split()):
                    return track
        tracks = results.get("tracks", [])
        return tracks[0] if tracks else None
    except Exception:
        return None


def import_to_tidal(
    session: tidalapi.Session,
    tracks: list[dict],
    tidal_playlist_id: str,
    dry_run: bool,
) -> tuple[list[dict], str, int, int]:
    playlist      = session.playlist(tidal_playlist_id)
    playlist_name = playlist.name
    existing_ids  = get_existing_tidal_ids(playlist)

    added_tracks = []
    not_found    = []
    skipped      = 0

    # Spotify returns newest→oldest; reverse so oldest is inserted first,
    # then each newer track lands at position 0 — keeping newest on top.
    reversed_tracks = list(reversed(tracks))

    for i, track in enumerate(reversed_tracks, 1):
        title  = track["title"]
        artist = track["artist"]
        result = find_tidal_track(session, title, artist)

        if result is None:
            not_found.append(track)
            log(f"  [{i}/{len(reversed_tracks)}] ❌ NOT FOUND: {title} — {artist}")
            continue

        if result.id in existing_ids:
            skipped += 1
            log(f"  [{i}/{len(reversed_tracks)}] ⏭  ALREADY IN PLAYLIST: {title} — {artist}")
            continue

        existing_ids.add(result.id)
        added_tracks.append(track)
        log(f"  [{i}/{len(reversed_tracks)}] {'🔎 [DRY-RUN]' if dry_run else '✅'} {title} — {artist}")

        if not dry_run:
            playlist.add([result.id], allow_duplicates=True, position=0)
            time.sleep(0.1)

    label = "Would add" if dry_run else "Added"
    log(f"\n✅ Done! {label} {len(added_tracks)} new track(s). "
        f"Skipped {skipped} already present, {len(not_found)} not found on Tidal.")

    if dry_run:
        log("[dry-run] No changes were made to the Tidal playlist.")

    if not_found:
        log("\n⚠️  Tracks not found on Tidal:")
        for t in not_found:
            log(f"    - {t['title']} — {t['artist']}")

    return added_tracks, playlist_name, skipped, len(not_found)

# ── TELEGRAM ──────────────────────────────────────────────────────────────────

def send_telegram(added_tracks: list[dict], playlist_name: str, skipped: int, not_found: int) -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_ID:
        log("[telegram] TELEGRAM_TOKEN or TELEGRAM_ID not set — skipping notification")
        return

    payload = json.dumps({
        "chat_id":    TELEGRAM_ID,
        "text":       f"✅ _{playlist_name}_\nAdded {len(added_tracks)} new track(s). Skipped {skipped} already present, {not_found} not found on Tidal.",
        "parse_mode": "Markdown",
    }).encode()

    try:
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10):
            log(f"[telegram] Notification sent ✓ ({len(added_tracks)} track(s))")
    except urllib.error.HTTPError as e:
        log(f"[telegram] HTTP {e.code} error: {e.read().decode(errors='replace')}")
    except Exception as e:
        log(f"[telegram] Failed to send notification: {e}")

# ── MAIN ──────────────────────────────────────────────────────────────────────

def main() -> None:
    args = parse_args()
    setup_logging(args)

    if args.dry_run:
        log("🔎 DRY-RUN mode — no changes will be made to Tidal.\n")

    spotify_tracks = scrape_spotify(args.spotify_playlist_id)

    log("\n🔐 Connecting to Tidal...")
    session = load_or_create_session()

    added_tracks, playlist_name, skipped, not_found = import_to_tidal(
        session, spotify_tracks, args.tidal_playlist_id, dry_run=args.dry_run
    )

    if not args.dry_run and added_tracks:
        send_telegram(added_tracks, playlist_name, skipped, not_found)


if __name__ == "__main__":
    main()