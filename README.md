# vod-aggregator

Gather Twitch footage of a Deadlock match from every streamer who played in it,
so the same moment can be edited from several perspectives.

Planned stages:

1. **Footage and streamer database**: track streamers, register and download
   VODs, recover deleted VODs, record live streams, import local files.
2. **Match sync** (this release): read the match id and game clock from the stream, fetch the
   demo from deadlock-api.com (or use your own), parse it with
   [boon](https://github.com/nock0015/boon) and line events up with footage.
3. **Clips**: the main streamer's kills and deaths, plus every interaction with
   other known streamers, cut from each perspective.
4. **DaVinci Resolve timeline export.**

## Install

Requires Python 3.11+, ffmpeg and [Tesseract OCR](https://tesseract-ocr.github.io/tessdoc/Installation.html)
(for reading the match id and clock off the stream).

```sh
pip install -e .            # add [live] for live recording with streamlink
```

## Usage

No Twitch API key is needed. VOD metadata comes from Twitch's public web API
and downloads go through yt-dlp. To use the official API instead, set
`TWITCH_CLIENT_ID` and `TWITCH_CLIENT_SECRET`.

Data (the SQLite database and downloads) goes in `./data`, or in
`VODAGG_DATA_DIR` if set.

```sh
# Streamers. Steam account ids are what match them to players in demos.
vodagg streamer add Mike --twitch mikeplays --steam 123456789
vodagg streamer update Mike --steam 987654321   # add an alt account
vodagg streamer list

# VODs
vodagg vod list mikeplays
vodagg vod add https://www.twitch.tv/videos/2290000000
vodagg footage download 1 --from 1:02:00 --to 1:05:30

# A deleted VOD, from the broadcast id and start time a tracker such as
# TwitchTracker shows for the stream
vodagg vod recover mikeplays 41234567890 2026-10-19T20:01:13Z

# Record a live stream; its broadcast id is saved for later recovery
vodagg record mikeplays

# A file saved some other way, such as with the voddownloader.com extension
vodagg footage import Mike ~/Downloads/mike_vod.mp4 --started 2026-10-19T20:01:13Z
```

### Match sync

```sh
# Which matches a VOD shows, read from the match id in the bottom-right corner
vodagg match scan 1

# Line the matches up with the footage. Each match's demo comes from
# deadlock-api.com, or pass --demo with a .dem/.dem.bz2 you have.
vodagg match sync 1
vodagg match sync 1 --match 118211297 --demo ~/Downloads/118211297.dem

# Kills, deaths and ults of known streamers, with times in their footage
vodagg match events 118211297
```

Sync reads the game clock off the stream (top middle) every 30 seconds, then
a few seconds of frames densely to catch the clock ticking over. boon's
pause-aware tick conversion turns each reading into a range for where the
match starts in the footage; the ranges are intersected, so the result is
usually within a fraction of a second, pauses included. Parsing a demo
records everyone who played, their kills, deaths and ults, and links known
streamers with no Steam id yet when their name matches a player's.

## Development

```sh
pip install -e .[dev]
pytest
```
