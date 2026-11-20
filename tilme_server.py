#!/usr/bin/env python3
"""Small development server for tilme Shadowing.

The transcript provider (youtube-transcript-api) is UNOFFICIAL. YouTube can
block it; publishing third-party captions may require creator permission.
For a public product, replace provider with a licensed transcript service.
"""
from __future__ import annotations
import argparse
import html
import json
import os
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{11}$")
GENRES = json.loads((Path(__file__).parent / 'genre_queries.json').read_text(encoding='utf-8'))
CACHE = {}  # (http status, mime, payload, expiry)
CACHE_LOCK = Lock()
CAPTION_TTL = 60 * 60  # Memory only; not a persistent copy of third-party text.
DISCOVERY_TTL = 60 * 30


def to_timestamp(seconds: float) -> str:
    millis = max(0, round(seconds * 1000))
    hours, millis = divmod(millis, 3600000)
    minutes, millis = divmod(millis, 60000)
    secs, millis = divmod(millis, 1000)
    return f'{hours:02}:{minutes:02}:{secs:02}.{millis:03}'


def convert_to_cues(snippets):
    """Group ASR fragments for reasonably short shadowing chunks (up to ~8s).

    The timestamp range of each group is derived from source caption metadata.
    Do not fabricate text when there is no caption track.
    """
    cues = []
    parts = []
    first = last = 0.0

    def flush():
        nonlocal parts
        if not parts:
            return
        line = re.sub(r'\s+', ' ', ' '.join(parts)).strip()
        if line:
            cues.append((round(first, 3), round(max(first + .15, last), 3), line))
        parts = []

    for snippet in snippets:
        raw = getattr(snippet, 'text', '') if not isinstance(snippet, dict) else snippet.get('text', '')
        start = getattr(snippet, 'start', 0) if not isinstance(snippet, dict) else snippet.get('start', 0)
        duration = getattr(snippet, 'duration', 0) if not isinstance(snippet, dict) else snippet.get('duration', 0)
        try:
            start = max(0.0, float(start))
            end = start + max(0.1, float(duration))
        except (ValueError, TypeError):
            continue
        line = re.sub(r'<[^>]+>', '', html.unescape(str(raw)))
        line = re.sub(r'\s+', ' ', line).strip()
        if not line or (line.startswith('[') and line.endswith(']')):
            continue
        if parts and (start > last + 1.35 or end - first > 8 or len(' '.join(parts)) + len(line) > 160):
            flush()
        if not parts:
            first = start
        parts.append(line)
        last = end
        if re.search(r'[.!?]["\']?$', line) and end - first >= 1.1:
            flush()
        if len(cues) >= 1200:
            break
    flush()
    return cues


def cues_to_vtt(cues):
    result = ['WEBVTT', '']
    for a, b, txt in cues:
        # Control newlines, VTT directions, and extraneous tags: text only.
        result.append(f'{to_timestamp(a)} --> {to_timestamp(b)}')
        result.append(txt.replace('-->', '→').replace('\n', ' '))
        result.append('')
    return ('\n'.join(result) + '\n').encode('utf-8')


def transcript_vtt(video_id):
    if not ID_PATTERN.fullmatch(video_id):
        return 400, 'application/json', json_bytes({'error': 'invalid_video_id'})
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
        # As of youtube-transcript-api >=1.2: fetch(...) returns iterable snippets.
        transcript = YouTubeTranscriptApi().fetch(video_id, languages=['en', 'en-US', 'en-GB'])
        cues = convert_to_cues(transcript)
        if not cues:
            return 404, 'application/json', json_bytes({'error': 'no_english_captions'})
        return 200, 'text/vtt; charset=utf-8', cues_to_vtt(cues)
    except ImportError:
        return 503, 'application/json', json_bytes({'error': 'youtube_transcript_api_not_installed'})
    except Exception as exc:
        kind = type(exc).__name__
        status = 503 if kind in {'RequestBlocked', 'IpBlocked', 'TooManyRequests', 'YouTubeRequestFailed'} else 404
        # Never return an exception body from an external source to the client.
        return status, 'application/json', json_bytes({'error': 'transcript_unavailable', 'reason': kind})


def json_bytes(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8')


def discover(genre):
    data = GENRES.get(genre)
    if not data:
        return 404, 'application/json', json_bytes({'error': 'unknown_genre'})
    key = os.environ.get('YOUTUBE_DATA_API_KEY', '').strip()
    if not key:
        return 503, 'application/json', json_bytes({'error': 'youtube_data_api_key_not_configured'})
    from urllib.parse import urlencode
    query = urlencode({
        'part': 'snippet', 'q': data['search'], 'type': 'video',
        'videoCaption': 'closedCaption', 'videoEmbeddable': 'true',
        'maxResults': 8, 'relevanceLanguage': 'en', 'safeSearch': 'moderate',
        'key': key,
    })
    try:
        req = Request('https://www.googleapis.com/youtube/v3/search?' + query,
                      headers={'User-Agent': 'tilme-shadowing-dev/1.0'})
        with urlopen(req, timeout=12) as response:
            decoded = json.load(response)
        videos = []
        for item in decoded.get('items', []):
            vid = item.get('id', {}).get('videoId', '')
            snip = item.get('snippet', {})
            if not ID_PATTERN.fullmatch(vid):
                continue
            videos.append({'id': vid,
                           'title': html.unescape(snip.get('title', '')).strip()[:150],
                           'source': html.unescape(snip.get('channelTitle', '')).strip()[:90],
                           'level': '—',  # Unknown; don't invent a CEFR level.
                           'genre': genre, 'genres': [genre],
                           'caption_priority': 'youtube_search_filter'})
        return 200, 'application/json', json_bytes({'genre': genre, 'videos': videos})
    except (TimeoutError, URLError, HTTPError, ValueError):
        return 503, 'application/json', json_bytes({'error': 'youtube_search_unavailable'})


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = urlparse(self.path).path
        if path == '/health':
            self.send_result(200, 'application/json', json_bytes({
                'status': 'ok', 'transcripts': 'experimental',
                'discovery': bool(os.environ.get('YOUTUBE_DATA_API_KEY'))}))
            return
        match = re.fullmatch(r'/api/transcript/([A-Za-z0-9_-]{11})\.vtt', path)
        if match:
            video = match.group(1)
            self.cached('caption:' + video, CAPTION_TTL, lambda: transcript_vtt(video))
            return
        match = re.fullmatch(r'/api/discover/([a-z_-]+)', path)
        if match:
            genre = match.group(1)
            self.cached('discover:' + genre, DISCOVERY_TTL, lambda: discover(genre))
            return
        self.send_result(404, 'application/json', json_bytes({'error': 'not_found'}))

    def cached(self, cache_key, ttl, producer):
        now = time.monotonic()
        with CACHE_LOCK:
            entry = CACHE.get(cache_key)
        if entry and now < entry[3]:
            self.send_result(*entry[:3])
            return
        status, mime, body = producer()
        # Don't cache temporary server failures for a full hour.
        timeout = ttl if status == 200 else (90 if status == 404 else 15)
        with CACHE_LOCK:
            CACHE[cache_key] = status, mime, body, now + timeout
        self.send_result(status, mime, body)

    def send_result(self, status, mime, data):
        self.send_response(status)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        # Only request paths are logged; never log transcript text.
        super().log_message(fmt, *args)


def main():
    parser = argparse.ArgumentParser(description='tilme local transcript API')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args()
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print('tilme caption server:', f'http://{args.host}:{args.port}', flush=True)
    print('NOTE: unofficial caption library may fail; check creator rights and YouTube terms before public launch.', flush=True)
    httpd.serve_forever()

if __name__ == '__main__':
    main()
