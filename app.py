import os
import time
import random
import json
import logging
import uuid
from datetime import datetime, timedelta
import zipfile
import io
from flask import Flask, request, render_template, send_from_directory, send_file, jsonify
from pydub import AudioSegment
from functools import wraps
import yt_dlp
from googleapiclient.discovery import build
from spotipy.oauth2 import SpotifyClientCredentials
import spotipy
from threading import Thread

# Flask app setup
app = Flask(__name__)
BASE_DOWNLOAD_FOLDER = './downloads'
app.config['BASE_DOWNLOAD_FOLDER'] = BASE_DOWNLOAD_FOLDER

# Logging setup
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Load configuration
with open('config.json', 'r') as f:
    config = json.load(f)

# Spotify API setup
sp = spotipy.Spotify(auth_manager=SpotifyClientCredentials(client_id=config['spotifyClientId'],
                                                            client_secret=config['spotifyClientSecret']))

# Retry decorator
def retry_on_failure(retries=5, backoff_factor=2, jitter=True):
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            attempt = 0
            while attempt < retries:
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    attempt += 1
                    delay = backoff_factor ** attempt
                    if jitter:
                        delay += random.uniform(0, 1)
                    logging.warning(f"Error in {func.__name__}: {e}. Retrying in {delay:.2f} seconds...")
                    time.sleep(delay)
            raise Exception(f"{func.__name__} failed after {retries} retries.")
        return wrapper
    return decorator

# Cleanup old files (older than 5 days)
def cleanup_old_files():
    while True:
        now = datetime.now()
        for folder in os.listdir(BASE_DOWNLOAD_FOLDER):
            folder_path = os.path.join(BASE_DOWNLOAD_FOLDER, folder)
            if os.path.isdir(folder_path):
                for file in os.listdir(folder_path):
                    file_path = os.path.join(folder_path, file)
                    if os.path.isfile(file_path):
                        file_age = now - datetime.fromtimestamp(os.path.getmtime(file_path))
                        if file_age > timedelta(days=5):
                            os.remove(file_path)
                            logging.info(f"Deleted old file: {file_path}")
                # Remove the folder if empty
                if not os.listdir(folder_path):
                    os.rmdir(folder_path)
        time.sleep(3600)  # Run cleanup every hour

# Fetch Spotify playlist tracks
@retry_on_failure()
def fetch_spotify_playlist_tracks(playlist_url):
    playlist_id = playlist_url.split('/playlist/')[1].split('?')[0]
    results = sp.playlist_tracks(playlist_id)
    tracks = []

    while results:
        for item in results['items']:
            track = item['track']
            tracks.append({'name': track['name'], 'artist': track['artists'][0]['name']})
        results = sp.next(results) if results['next'] else None

    logging.info(f"Fetched {len(tracks)} tracks from Spotify.")
    return tracks

# Search YouTube for a video using the YouTube API (primary method)
@retry_on_failure()
def search_youtube_api(query):
    youtube = build("youtube", "v3", developerKey=random.choice(config["youtubeApiKeys"]))
    search_response = youtube.search().list(q=query, part="id,snippet", maxResults=1).execute()

    if 'items' in search_response and search_response['items']:
        video_url = "https://www.youtube.com/watch?v=" + search_response['items'][0]['id']['videoId']
        logging.info(f"Found video for '{query}' using YouTube API: {video_url}")
        return video_url
    else:
        raise Exception(f"No YouTube results found for '{query}'")

# Search YouTube using yt-dlp (fallback method)
@retry_on_failure()
def search_youtube_yt_dlp(query):
    logging.info(f"Searching for '{query}' on YouTube using yt-dlp...")
    ydl_opts = {'quiet': True, 'extractaudio': True, 'format': 'bestaudio/best', 'noplaylist': True}
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        result = ydl.extract_info(f"ytsearch:{query}", download=False)
        if 'entries' in result and len(result['entries']) > 0:
            video_url = result['entries'][0]['url']
            logging.info(f"Found video for '{query}' using yt-dlp: {video_url}")
            return video_url
        else:
            raise Exception(f"No results found for '{query}'")

# Download a song from YouTube
@retry_on_failure()
def download_song(video_url, query, output_dir):
    output_path = os.path.join(output_dir, f"{query}.webm")
    ydl_opts = {'format': 'bestaudio/best', 'outtmpl': output_path, 'noplaylist': False}

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        logging.info(f"Downloading {query}...")
        ydl.download([video_url])
    logging.info(f"Downloaded {query} successfully.")
    convert_to_mp3(output_path, query, output_dir)

# Convert downloaded audio to MP3
def convert_to_mp3(input_file, query, output_dir):
    try:
        logging.info(f"Converting {query} to MP3...")
        audio = AudioSegment.from_file(input_file)
        mp3_output_path = os.path.join(output_dir, f"{query}.mp3")
        audio.export(mp3_output_path, format="mp3")
        os.remove(input_file)
        logging.info(f"Converted {query} to MP3 successfully.")
    except Exception as e:
        logging.error(f"Error converting {query} to MP3: {e}")

@app.route('/', methods=['GET', 'POST'])
def index():
    if request.method == 'POST':
        playlist_url = request.form['playlist_url']
        user_id = str(uuid.uuid4())
        user_folder = os.path.join(BASE_DOWNLOAD_FOLDER, user_id)
        os.makedirs(user_folder, exist_ok=True)

        # Start downloading tracks
        try:
            tracks = fetch_spotify_playlist_tracks(playlist_url)
            for track in tracks:
                query = f"{track['name']} {track['artist']}"
                try:
                    try:
                        video_url = search_youtube_api(query)
                    except Exception:
                        video_url = search_youtube_yt_dlp(query)
                    download_song(video_url, query, user_folder)
                except Exception as e:
                    logging.error(f"Failed to download {query}: {e}")
            return jsonify({"status": "success", "user_id": user_id, "message": "Download complete!"})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})

    return '''
    <form method="POST">
        Spotify Playlist URL: <input type="text" name="playlist_url">
        <button type="submit">Download</button>
    </form>
    '''

@app.route('/files/<user_id>')
def list_files(user_id):
    user_folder = os.path.join(BASE_DOWNLOAD_FOLDER, user_id)
    if not os.path.exists(user_folder):
        return jsonify({"status": "error", "message": "No files found for this user."})

    files = [f for f in os.listdir(user_folder) if f.endswith('.mp3')]
    return jsonify({"files": files})

@app.route('/files/<user_id>/download_all')
def download_all(user_id):
    user_folder = os.path.join(BASE_DOWNLOAD_FOLDER, user_id)
    if not os.path.exists(user_folder):
        return jsonify({"status": "error", "message": "No files found for this user."})

    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
        for filename in os.listdir(user_folder):
            if filename.endswith('.mp3'):
                zip_file.write(os.path.join(user_folder, filename), filename)
    zip_buffer.seek(0)
    return send_file(zip_buffer, as_attachment=True, download_name=f"{user_id}_songs.zip", mimetype="application/zip")

if __name__ == '__main__':
    # Start cleanup in a separate thread
    Thread(target=cleanup_old_files, daemon=True).start()
    app.run(host='0.0.0.0', port=8080)
