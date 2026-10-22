"""
Spotify Playlist Scraper → Tidal Importer
==========================================
Uses SpotAPI (no Selenium, no API key) to fetch all tracks from a public
Spotify playlist, then imports only NEW tracks into an existing Tidal playlist.

Matching is strict: artist and title must both match. Tracks without a
confident match are listed in not_matched.json (with Tidal's closest hit).
Everything the script adds is recorded in added_history.json; if such a track
later disappears from the Tidal playlist it is moved to removed_tracks.json
and never re-added.

Requirements:
    pip install -r requirements.txt

.env file:
    TELEGRAM_TOKEN=your_bot_token
    TELEGRAM_ID=your_chat_id

Usage:
    python spotify2tidal.py <spotify_playlist_id> <tidal_playlist_id> [--dry-run]
                            [--logtoscreen | --logtofile | --lognone]
"""

import argparse
import json
import logging
import os
import pathlib
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from datetime import datetime
from difflib import SequenceMatcher

import tidalapi
from dotenv import load_dotenv
from spotapi import PublicPlaylist

# ── CONFIG ────────────────────────────────────────────────────────────────────

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_ID    = os.getenv("TELEGRAM_ID", "")
SESSION_FILE   = pathlib.Path("tidal_session.json")
LOG_FILE       = pathlib.Path("spotify_to_tidal.log")
# State files (JSON). History = everything this script ever added; removed =
# tracks that were in history but later disappeared from the playlist (deleted
# by you) and are never re-added; not_matched = tracks without a confident
# Tidal match, for manual follow-up.
HISTORY_FILE     = pathlib.Path("added_history.json")
REMOVED_FILE     = pathlib.Path("removed_tracks.json")
NOT_MATCHED_FILE = pathlib.Path("not_matched.json")
# Minimum similarity (0-1) for artist / title to count as a match
MATCH_THRESHOLD  = 0.85

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
        help="Print log output to the screen (default when no other log option is given).",
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

            except Exception as e:
                log(f"  ⚠️  Could not parse Spotify item ({e}) — skipping")
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
    try:
        SESSION_FILE.chmod(0o600)  # tokens are stored in plain text
    except OSError:
        pass
    log(f"[tidal] Session saved to {SESSION_FILE}")
    return session

# ── TIDAL IMPORT ──────────────────────────────────────────────────────────────

def normalize(text: str) -> str:
    """Lowercase, strip accents, bracketed parts, remaster/live suffixes and punctuation."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    stripped = re.sub(r"\s*[\(\[].*?[\)\]]", "", text)
    stripped = re.sub(r"\s-\s.*(remaster|live|version|mix|edit|mono|stereo).*$", "", stripped)
    if stripped.strip():
        text = stripped
    text = text.replace("&", " and ")
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def similar(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


def track_artist_names(track) -> list[str]:
    artists = getattr(track, "artists", None) or ([track.artist] if track.artist else [])
    return [normalize(a.name) for a in artists if a and a.name]


def artist_matches(spotify_artist: str, track) -> bool:
    tidal_names = track_artist_names(track)
    if not tidal_names:
        return False
    # Spotify joins multiple artists with ", "; accept a match on any single one
    # as well as on the full combined string.
    sp_names = [normalize(a) for a in spotify_artist.split(", ") if a.strip()]
    parts = [normalize(spotify_artist)] + sp_names
    candidates = tidal_names + [" and ".join(tidal_names)]
    return any(similar(p, c) >= MATCH_THRESHOLD for p in parts for c in candidates)


def title_matches(spotify_title: str, track) -> bool:
    return similar(normalize(spotify_title), normalize(track.name)) >= MATCH_THRESHOLD


def find_tidal_track(session: tidalapi.Session, title: str, artist: str):
    """
    Return (match, closest). `match` is a Tidal track only when both artist and
    title match confidently, otherwise None. `closest` is the first search hit
    (or None) so it can be suggested for manual follow-up.
    """
    try:
        results = session.search(f"{title} {artist}", models=[tidalapi.Track], limit=10)
    except Exception as e:
        log(f"  ⚠️  Tidal search failed for {title} — {artist}: {e}")
        return None, None

    tracks = results.get("tracks", [])
    if not tracks:
        return None, None

    for track in tracks:
        if artist_matches(artist, track) and title_matches(title, track):
            return track, tracks[0]
    return None, tracks[0]


def load_json(path: pathlib.Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        log(f"[state] Could not read {path} ({e}) — starting empty")
        return {}


def save_json(path: pathlib.Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def get_existing_tidal_ids(playlist: tidalapi.Playlist) -> set[int]:
    log("📋 Fetching existing tracks from Tidal playlist...")
    ids = {track.id for track in playlist.tracks_paginated()}
    log(f"   {len(ids)} tracks already in playlist.")
    return ids


def import_to_tidal(
    session: tidalapi.Session,
    tracks: list[dict],
    tidal_playlist_id: str,
    dry_run: bool,
) -> tuple[list[dict], str, int, int]:
    playlist      = session.playlist(tidal_playlist_id)
    playlist_name = playlist.name
    existing_ids  = get_existing_tidal_ids(playlist)

    history     = load_json(HISTORY_FILE)
    removed     = load_json(REMOVED_FILE)
    not_matched = load_json(NOT_MATCHED_FILE)
    today       = datetime.now().strftime("%Y-%m-%d")
    current_ids = {str(i) for i in existing_ids}

    # Tracks you put back in the playlist yourself are no longer "removed"
    for tid in [t for t in removed if t in current_ids]:
        log(f"[history] Back in playlist, no longer treated as removed: "
            f"{removed[tid].get('artist')} — {removed[tid].get('title')}")
        del removed[tid]
    # Tracks this script added earlier that are now gone were removed by you
    for tid, info in history.items():
        if tid not in current_ids and tid not in removed:
            removed[tid] = {**info, "removed_detected": today}
            log(f"[history] Removed by you, will not be re-added: "
                f"{info.get('artist')} — {info.get('title')} (id={tid})")

    added_tracks = []
    not_found    = []
    skipped      = 0

    # Spotify returns newest→oldest; reverse so oldest is inserted first,
    # then each newer track lands at position 0 — keeping newest on top.
    reversed_tracks = list(reversed(tracks))

    try:
        for i, track in enumerate(reversed_tracks, 1):
            title  = track["title"]
            artist = track["artist"]
            key    = f"{artist} — {title}"
            result, closest = find_tidal_track(session, title, artist)

            if result is None:
                not_found.append(track)
                log(f"  [{i}/{len(reversed_tracks)}] ❌ NO CONFIDENT MATCH: {title} — {artist}")
                not_matched[key] = {
                    "artist": artist,
                    "title":  title,
                    "first_seen": not_matched.get(key, {}).get("first_seen", today),
                    "closest_tidal_hit": {
                        "artist": closest.artist.name if closest.artist else None,
                        "title":  closest.name,
                        "id":     closest.id,
                    } if closest else None,
                }
                continue

            not_matched.pop(key, None)

            if str(result.id) in removed:
                skipped += 1
                log(f"  [{i}/{len(reversed_tracks)}] ⊘ PREVIOUSLY REMOVED BY YOU: {title} — {artist}")
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
                history[str(result.id)] = {
                    "artist": result.artist.name if result.artist else artist,
                    "title":  result.name,
                    "spotify": key,
                    "added_on": today,
                }
                time.sleep(0.1)
    finally:
        if not dry_run:
            save_json(HISTORY_FILE, history)
            save_json(REMOVED_FILE, removed)
            save_json(NOT_MATCHED_FILE, not_matched)
            log(f"[state] Saved {HISTORY_FILE}, {REMOVED_FILE}, {NOT_MATCHED_FILE}")

    label = "Would add" if dry_run else "Added"
    log(f"\n✅ Done! {label} {len(added_tracks)} new track(s). "
        f"Skipped {skipped} already present or removed, {len(not_found)} without a confident match.")

    if dry_run:
        log("[dry-run] No changes were made to the Tidal playlist.")

    if not_found:
        log(f"\n⚠️  Tracks without a confident Tidal match (see {NOT_MATCHED_FILE}):")
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
        "text":       f"✅ _{playlist_name}_\nAdded {len(added_tracks)} new track(s). Skipped {skipped} already present, {not_found} without a confident match.",
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