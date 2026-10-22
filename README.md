# spotify2tidal

Fetches all tracks from a public Spotify playlist and adds the new ones to an existing TIDAL playlist — newest on top.

The script handles the full flow: reading the Spotify playlist via SpotAPI (no Selenium, no API key), searching each track on TIDAL with strict matching, skipping tracks already in the playlist, remembering what it added, and sending a Telegram notification when new tracks are added.

---

## How it works

1. Fetches the public Spotify playlist via SpotAPI and deduplicates by title and artist
2. Searches each track on TIDAL
3. Skips tracks already in the playlist, and tracks you previously removed
4. Adds new tracks to your playlist oldest first, so the newest ends up on top (TIDAL stacks additions)
5. Records what it added; tracks you later delete from the playlist are never re-added
6. Writes songs without a confident TIDAL match to `not_matched_<tidal_playlist_id>.json`
7. Sends a Telegram notification with a summary (only when tracks were actually added)

---

## Logging

Log output goes to the screen by default. Use `--logtofile` to write to `spotify_to_tidal.log` instead, or `--lognone` to suppress it. The TIDAL login URL is always printed to the screen.

---

## Requirements

```bash
pip install -r requirements.txt
```

This installs [SpotAPI](https://github.com/Aran404/SpotAPI), [tidalapi](https://github.com/EbbLabs/python-tidal) and python-dotenv.

---

## Setup

Create a `.env` file in the same directory:

```
TELEGRAM_TOKEN=your-telegram-bot-token
TELEGRAM_ID=your-telegram-chat-id
```

**Spotify** — the playlist must be public. The ID is the part of the playlist URL after `/playlist/`.

**TIDAL** — find your playlist ID by opening the playlist in TIDAL and copying the UUID from the URL.

**Telegram** — `TELEGRAM_TOKEN` and `TELEGRAM_ID` are optional. If omitted, notifications are skipped.
- Create a bot and get a token via [@BotFather](https://t.me/BotFather)
- Get your chat ID by messaging your bot, then visiting `https://api.telegram.org/bot<TOKEN>/getUpdates`

---

## Usage

```bash
# Import new tracks
python spotify2tidal.py <spotify_playlist_id> <tidal_playlist_id>

# Dry run — searches TIDAL, but does not modify the playlist
python spotify2tidal.py <spotify_playlist_id> <tidal_playlist_id> --dry-run

# Log to a file, or not at all (default: screen)
python spotify2tidal.py <spotify_playlist_id> <tidal_playlist_id> --logtofile
python spotify2tidal.py <spotify_playlist_id> <tidal_playlist_id> --lognone
```

---

## Removing doubles from a playlist

`dedupe_tidal.py` finds songs that appear more than once in a TIDAL playlist (for example album vs single vs remaster) and removes the doubles, keeping the **oldest** one: the copy that was added to the playlist first.

```bash
# Report only (default): shows what would be kept and removed
python dedupe_tidal.py <tidal_playlist_id>

# Actually remove the doubles
python dedupe_tidal.py <tidal_playlist_id> --apply

# Keep live/remix versions apart (don't ignore bracketed text in titles)
python dedupe_tidal.py <tidal_playlist_id> --strict
```

- Songs are the same when normalized title and primary artist are equal. By default bracketed text like `(Live)` or `(Remastered 2011)` is ignored; `--strict` keeps it
- Oldest = earliest date added to the playlist. If TIDAL gives no dates, the lowest position counts as oldest (new tracks are added on top)
- Without `--apply` nothing is changed. With `--apply` the removed tracks are first saved to `dedupe_removed_<tidal_playlist_id>_<time>.json`
- The importer uses the same title-and-artist check, so it no longer adds a second version of a song that is already in the playlist

---

## Telegram notifications

When tracks are added, a message is sent to your Telegram bot:

```
✅ My Playlist
Added 3 new track(s). Skipped 120 already present or removed, 2 without a confident match.
```

No message is sent when there is nothing new to add, when running with `--dry-run`, or when the Telegram variables are not set.

---

## Re-running on an existing playlist

Safe to run repeatedly. On each run:

- Tracks already in the playlist are skipped (`⏭ ALREADY IN PLAYLIST`)
- Only songs new on the Spotify playlist since the last run are added
- Matching is strict (artist and title must both match, similarity ≥ 0.85). Songs without a confident match are not added; they are listed in `not_matched_<tidal_playlist_id>.json` with TIDAL's closest hit so you can add them manually
- Everything the script adds is stored in `added_history_<tidal_playlist_id>.json`. If you delete such a track from the playlist, the next run detects that, moves it to `removed_tracks_<tidal_playlist_id>.json` and never re-adds it. Putting it back by hand clears it from that list
- The history starts at the first run with this version, so tracks deleted before that are not known as removed
- State is kept per Tidal playlist (the ID is in the file name), so you can manage several playlists with the same script without them affecting each other
- With `--dry-run` none of these files are written

---

## Authentication

On first run you will be asked to log in to TIDAL via a URL printed in the terminal. Open it in your browser and complete the login. The session is saved to `tidal_session.json` so you only need to do this once.

---

## Config

At the top of `spotify2tidal.py`:

| Variable | Default | Description |
|---|---|---|
| `MATCH_THRESHOLD` | `0.85` | Minimum artist/title similarity (0–1) for a match |
| `TELEGRAM_TOKEN` | — | Telegram bot token (from `.env`) |
| `TELEGRAM_ID` | — | Telegram chat ID (from `.env`) |

---

## Files

| File | Description |
|---|---|
| `spotify2tidal.py` | Main script |
| `dedupe_tidal.py` | Removes duplicate songs from a TIDAL playlist, keeping the oldest |
| `requirements.txt` | Python dependencies |
| `.env` | Your credentials — not committed |
| `tidal_session.json` | Saved TIDAL OAuth session — not committed |
| `spotify_to_tidal.log` | Run log, only with `--logtofile` |
| `added_history_<tidal_playlist_id>.json` | Tracks the script has added (id, artist, title, date) |
| `removed_tracks_<tidal_playlist_id>.json` | Tracks you removed from the playlist; never re-added |
| `not_matched_<tidal_playlist_id>.json` | Tracks without a confident TIDAL match, plus closest hit |
| `dedupe_removed_<tidal_playlist_id>_<time>.json` | Backup of tracks removed by `dedupe_tidal.py --apply` |

---

## .gitignore

```
.env
tidal_session.json
*.log
added_history*.json
removed_tracks*.json
not_matched*.json
dedupe_removed_*.json
__pycache__/
.venv/
```
