"""
Tidal playlist de-duplicator
============================
Finds songs that appear more than once in a Tidal playlist (same song under
different Tidal IDs, e.g. album vs single vs remaster, or the very same ID
twice) and removes the doubles, keeping the OLDEST one: the copy that was
added to the playlist first.

Nothing is removed unless --apply is given; without it the script only reports.
Before removing, the removed tracks are saved to dedupe_removed_<id>_<time>.json.

Two tracks count as the same song when normalized title and primary artist are
equal. Bracketed text like "(Live)" or "(Remastered 2011)" is ignored unless
--strict is given, which keeps live/remix versions apart.

Usage:
    python dedupe_tidal.py <tidal_playlist_id> [--apply] [--strict]
"""

import argparse
import json
import logging
import sys
from datetime import datetime

from spotify2tidal import load_or_create_session, logger, song_key

DATE_FMT = "%Y-%m-%d %H:%M"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Remove duplicate songs from a Tidal playlist, keeping the oldest."
    )
    parser.add_argument(
        "tidal_playlist_id",
        help="Tidal playlist UUID (e.g. ea35a54e-2a76-4f7d-b942-e2e4e228fd48)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually remove the doubles. Without it the script only reports.",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Keep bracketed text like (Live) or (Remix) when comparing titles.",
    )
    return parser.parse_args()


def find_duplicates(tracks: list, strict: bool = False) -> list[tuple[int, list[int]]]:
    """
    Group playlist positions by song. Returns, for each song that occurs more
    than once, (index to keep, [indices to remove]).

    Oldest = earliest `user_date_added`. Without dates for all copies, fall back
    to position: new tracks are inserted at the top, so the lowest one is oldest.
    """
    groups: dict[tuple[str, str], list[int]] = {}
    for index, track in enumerate(tracks):
        groups.setdefault(song_key(track, keep_brackets=strict), []).append(index)

    result = []
    for indices in groups.values():
        if len(indices) < 2:
            continue
        if all(getattr(tracks[i], "user_date_added", None) for i in indices):
            keep = min(indices, key=lambda i: (tracks[i].user_date_added, -i))
        else:
            keep = max(indices)
        result.append((keep, [i for i in indices if i != keep]))
    return sorted(result)


def describe(track, index: int) -> str:
    added  = getattr(track, "user_date_added", None)
    when   = added.strftime(DATE_FMT) if added else "unknown date"
    artist = track.artist.name if track.artist else "?"
    album  = track.album.name if getattr(track, "album", None) else "?"
    return f"#{index + 1}  {artist} — {track.name}  [{album}]  id={track.id}  added {when}"


def main() -> None:
    args = parse_args()

    logger.setLevel(logging.INFO)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)

    session  = load_or_create_session()
    playlist = session.playlist(args.tidal_playlist_id)
    tracks   = list(playlist.tracks_paginated())
    print(f"Playlist '{playlist.name}': {len(tracks)} tracks.")

    duplicates = find_duplicates(tracks, strict=args.strict)
    if not duplicates:
        print("No duplicates found.")
        return

    to_remove = [i for _, removals in duplicates for i in removals]
    for keep, removals in duplicates:
        print()
        print(f"  KEEP    {describe(tracks[keep], keep)}")
        for i in removals:
            print(f"  REMOVE  {describe(tracks[i], i)}")
    print(f"\n{len(duplicates)} song(s) with doubles, {len(to_remove)} track(s) to remove.")

    if not args.apply:
        print("Report only. Run again with --apply to remove them.")
        return

    backup = f"dedupe_removed_{args.tidal_playlist_id}_{datetime.now():%Y%m%d_%H%M%S}.json"
    with open(backup, "w", encoding="utf-8") as f:
        json.dump(
            [
                {
                    "position": i + 1,
                    "id": tracks[i].id,
                    "artist": tracks[i].artist.name if tracks[i].artist else None,
                    "title": tracks[i].name,
                    "added": tracks[i].user_date_added.isoformat()
                             if getattr(tracks[i], "user_date_added", None) else None,
                }
                for i in sorted(to_remove)
            ],
            f, indent=2, ensure_ascii=False,
        )
    print(f"Removed tracks saved to {backup}")

    # One request with the original positions, so indices cannot shift mid-way.
    if playlist.remove_by_indices(sorted(to_remove)):
        print(f"✅ Removed {len(to_remove)} track(s).")
    else:
        print("❌ Tidal rejected the removal; nothing was changed.")
        sys.exit(1)


if __name__ == "__main__":
    main()
