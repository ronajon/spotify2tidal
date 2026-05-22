# Spotify Playlist Scraper → Tidal Importer

Uses SpotAPI (no Selenium, no API key) to fetch all tracks from a public
Spotify playlist, then imports only NEW tracks into an existing Tidal playlist.

## Requirements:

```bash
pip install spotapi tidalapi pymongo redis websockets python-dotenv
```

## Setup:

create a `.env` file in the same directory:

```
TELEGRAM_TOKEN=your_bot_token
TELEGRAM_ID=your_chat_id
```

## Usage:

```bash
python spotify_to_tidal.py <spotify_playlist_id> <tidal_playlist_id> 

Do not write tracks to Tidal [--dry-run] 
Log to screen or file of no log [--logtoscreen | --logtofile | --lognone]
```