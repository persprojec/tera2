import os
import time
import subprocess
from http.cookiejar import MozillaCookieJar

import requests
from flask import (
    Flask, request, Response, jsonify,
    send_from_directory, redirect, url_for
)
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from TeraboxDL import TeraboxDL

# -------------- Config --------------
COOKIE_NAMES   = ("lang", "ndus")
COOKIES_FILE   = "cookies.txt"
CHUNK_SIZE     = 64 * 1024   # 64 KiB for /stream proxy
INFO_CACHE_TTL = 300         # seconds
HLS_ROOT       = "hls_cache" # where we store generated HLS sets
FFMPEG_CMD     = "ffmpeg"    # path to your ffmpeg binary

# -------------- App setup --------------
app = Flask(__name__, static_folder='.', template_folder='.')

# Simple in-memory cache for /info
_info_cache = {}

def get_cached_info(share_url):
    entry = _info_cache.get(share_url)
    if not entry:
        return None
    ts, data = entry
    if time.time() - ts > INFO_CACHE_TTL:
        del _info_cache[share_url]
        return None
    return data

def set_cached_info(share_url, data):
    _info_cache[share_url] = (time.time(), data)

def make_retry_session(total_retries=5, backoff_factor=1,
                       status_forcelist=(500,502,503,504)):
    sess = requests.Session()
    retry = Retry(
        total=total_retries,
        backoff_factor=backoff_factor,
        status_forcelist=status_forcelist,
        allowed_methods=frozenset(['GET','POST']),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    sess.mount("http://", adapter)
    sess.mount("https://", adapter)
    return sess

# one persistent session for all proxying
UPSTREAM = make_retry_session()

def load_tb_cookie_string(cookies_file=COOKIES_FILE):
    if not os.path.isfile(cookies_file):
        return ""
    cj = MozillaCookieJar(cookies_file)
    try:
        cj.load(ignore_discard=True, ignore_expires=True)
    except:
        return ""
    return "; ".join(f"{c.name}={c.value}" for c in cj if c.name in COOKIE_NAMES)

# -------------- Ensure HLS directory --------------
os.makedirs(HLS_ROOT, exist_ok=True)

# -------------- Routes --------------

@app.route("/")
def index():
    return app.send_static_file("index.html")

@app.route("/signed-url")
def signed_url():
    share_url = request.args.get("share_url", "").strip()
    if not share_url:
        return jsonify({"error": "Missing `share_url` parameter"}), 400
    tb = TeraboxDL(load_tb_cookie_string())
    info = tb.get_file_info(share_url)
    if info.get("error"):
        return jsonify({"error": info["error"]}), 500
    dl_link = info.get("download_link") or info.get("downloadLink")
    if not dl_link:
        return jsonify({"error": "No download link returned"}), 500
    return jsonify({"url": dl_link})

@app.route("/info")
def file_info():
    share_url = request.args.get("share_url", "").strip()
    if not share_url:
        return jsonify({"error": "Missing `share_url` parameter"}), 400

    # Try cache
    cached = get_cached_info(share_url)
    if cached:
        return jsonify(cached)

    tb   = TeraboxDL(load_tb_cookie_string())
    info = tb.get_file_info(share_url)
    if info.get("error"):
        return jsonify({"error": info["error"]}), 500

    filename = info.get("file_name") or info.get("filename") or "video"
    size_val = info.get("file_size") or info.get("size") or info.get("filesize") or 0
    try:
        size = float(size_val)
        for unit in ('B','KB','MB','GB','TB'):
            if size < 1024:
                size_str = f"{size:.1f}{unit}"
                break
            size /= 1024.0
    except:
        size_str = str(size_val)

    data = {"name": filename, "size": size_str}
    set_cached_info(share_url, data)
    return jsonify(data)

@app.route("/stream")
def stream_video():
    share_url = request.args.get("share_url", "").strip()
    if not share_url:
        return "Missing `share_url` parameter", 400

    tb   = TeraboxDL(load_tb_cookie_string())
    info = tb.get_file_info(share_url)
    if info.get("error"):
        return f"Error: {info['error']}", 500

    dl_link = info.get("download_link") or info.get("downloadLink")
    if not dl_link:
        return "Error: no download link found", 500

    headers = {}
    cookie_str = load_tb_cookie_string()
    if cookie_str:
        headers["Cookie"] = cookie_str
    if "Range" in request.headers:
        headers["Range"] = request.headers["Range"]

    upstream = UPSTREAM.get(dl_link, headers=headers, stream=True, timeout=10)
    excluded = {"Transfer-Encoding", "Connection", "Content-Encoding"}
    resp_headers = {
        k: v for k, v in upstream.headers.items() if k not in excluded
    }

    def gen():
        for chunk in upstream.iter_content(chunk_size=CHUNK_SIZE):
            if chunk:
                yield chunk

    return Response(gen(),
                    status=upstream.status_code,
                    headers=resp_headers)

@app.route("/download")
def download_video():
    share_url = request.args.get("share_url", "").strip()
    if not share_url:
        return "Missing `share_url` parameter", 400

    tb   = TeraboxDL(load_tb_cookie_string())
    info = tb.get_file_info(share_url)
    if info.get("error"):
        return f"Error: {info['error']}", 500

    dl_link = info.get("download_link") or info.get("downloadLink")
    if not dl_link:
        return "Error: no download link found", 500

    headers = {}
    cookie_str = load_tb_cookie_string()
    if cookie_str:
        headers["Cookie"] = cookie_str

    upstream = UPSTREAM.get(dl_link, headers=headers, stream=True, timeout=10)
    excluded = {"Transfer-Encoding", "Connection", "Content-Encoding"}
    resp_headers = {
        k: v for k, v in upstream.headers.items() if k not in excluded
    }

    filename = info.get("file_name") or info.get("filename") or "video"
    resp_headers["Content-Disposition"] = f'attachment; filename="{filename}"'

    def gen():
        for chunk in upstream.iter_content(chunk_size=CHUNK_SIZE):
            if chunk:
                yield chunk

    return Response(gen(),
                    status=upstream.status_code,
                    headers=resp_headers)

def ensure_hls(share_url, dl_link):
    """
    If HLS set for this share_url doesn't exist, generate it via ffmpeg.
    HLS folder: hls_cache/{url_hash}/
    Master playlist: master.m3u8
    """
    import hashlib
    key = hashlib.sha1(share_url.encode()).hexdigest()
    outdir = os.path.join(HLS_ROOT, key)
    if os.path.isdir(outdir) and os.path.isfile(os.path.join(outdir, "master.m3u8")):
        return key  # already generated

    os.makedirs(outdir, exist_ok=True)
    # Run ffmpeg to generate 3 renditions & master playlist
    cmd = [
        FFMPEG_CMD, "-y", "-i", dl_link,
        "-filter:v:0", "scale=640:360",  "-c:v:0", "libx264", "-b:v:0", "800k",
        "-filter:v:1", "scale=854:480",  "-c:v:1", "libx264", "-b:v:1", "1200k",
        "-filter:v:2", "scale=1280:720", "-c:v:2", "libx264", "-b:v:2", "2500k",
        "-map", "0:v", "-map", "0:a",
        "-f", "hls",
        "-hls_time", "6",
        "-hls_playlist_type", "vod",
        "-hls_segment_filename", os.path.join(outdir, "seg_%v_%03d.ts"),
        "-master_pl_name", "master.m3u8",
        "-var_stream_map", "v:0,a:0 v:1,a:0 v:2,a:0",
        os.path.join(outdir, "stream_%v.m3u8")
    ]
    # This will block until ffmpeg finishes
    subprocess.check_call(cmd)
    return key

@app.route("/hls/<share_id>/master.m3u8")
def hls_master(share_id):
    """
    Trigger HLS generation (if needed) then serve the master playlist.
    share_id is a URL-encoded share_url
    """
    share_url = request.args.get("share_url", share_id)
    tb        = TeraboxDL(load_tb_cookie_string())
    info      = tb.get_file_info(share_url)
    if info.get("error"):
        return jsonify({"error": info["error"]}), 500
    dl_link = info.get("download_link") or info.get("downloadLink")
    if not dl_link:
        return jsonify({"error": "no download link"}), 500

    key = ensure_hls(share_url, dl_link)
    return send_from_directory(os.path.join(HLS_ROOT, key),
                               "master.m3u8",
                               mimetype="application/vnd.apple.mpegurl")

@app.route("/hls/<key>/<path:filename>")
def hls_segments(key, filename):
    """
    Serve any .ts or .m3u8 file from the HLS cache.
    """
    return send_from_directory(os.path.join(HLS_ROOT, key),
                               filename,
                               conditional=True)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 4000))
    app.run(host="0.0.0.0", port=port, threaded=True)
