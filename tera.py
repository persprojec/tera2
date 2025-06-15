# server.py
import os
import requests
from http.cookiejar import MozillaCookieJar
from urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter
from flask import Flask, request, Response, jsonify
from TeraboxDL import TeraboxDL

COOKIE_NAMES = ("lang", "ndus")
COOKIES_FILE = "cookies.txt"

def make_retry_session(total_retries=5, backoff_factor=1,
                       status_forcelist=(500,502,503,504), session=None):
    session = session or requests.Session()
    retry = Retry(
        total=total_retries,
        backoff_factor=backoff_factor,
        status_forcelist=status_forcelist,
        allowed_methods=frozenset(['GET','POST']),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session

def load_tb_cookie_string(cookies_file=COOKIES_FILE):
    if not os.path.isfile(cookies_file):
        return ""
    cj = MozillaCookieJar(cookies_file)
    try:
        cj.load(ignore_discard=True, ignore_expires=True)
    except:
        return ""
    return "; ".join(f"{c.name}={c.value}" for c in cj if c.name in COOKIE_NAMES)

def human_readable_size(num, suffix='B'):
    """Convert bytes to human-readable string."""
    for unit in ['','K','M','G','T','P']:
        if abs(num) < 1024.0:
            return f"{num:.1f}{unit}{suffix}"
        num /= 1024.0
    return f"{num:.1f}Y{suffix}"

app = Flask(__name__, static_folder='.', template_folder='.')

@app.route("/")
def index():
    return app.send_static_file("index.html")

@app.route("/stream")
def stream_video():
    share_url = request.args.get("share_url", "").strip()
    if not share_url:
        return "Missing `share_url` parameter", 400

    cookie_str = load_tb_cookie_string()
    tb = TeraboxDL(cookie_str)
    info = tb.get_file_info(share_url)
    if info.get("error"):
        return f"Error: {info['error']}", 500

    dl_link = info.get("download_link") or info.get("downloadLink")
    if not dl_link:
        return "Error: no download link found", 500

    upstream_headers = {}
    if cookie_str:
        upstream_headers["Cookie"] = cookie_str
    range_hdr = request.headers.get("Range")
    if range_hdr:
        upstream_headers["Range"] = range_hdr

    upstream = make_retry_session().get(
        dl_link, headers=upstream_headers, stream=True, timeout=10
    )

    def generate():
        for chunk in upstream.iter_content(chunk_size=8192):
            if chunk:
                yield chunk

    excluded = {"Transfer-Encoding", "Connection", "Content-Encoding"}
    headers = {k: v for k, v in upstream.headers.items() if k not in excluded}

    return Response(generate(), status=upstream.status_code, headers=headers)

@app.route("/info")
def file_info():
    share_url = request.args.get("share_url", "").strip()
    if not share_url:
        return jsonify({"error": "Missing `share_url` parameter"}), 400

    cookie_str = load_tb_cookie_string()
    tb = TeraboxDL(cookie_str)
    info = tb.get_file_info(share_url)
    if info.get("error"):
        return jsonify({"error": info["error"]}), 500

    # Adjust these keys if your TeraboxDL returns different names
    filename = info.get("file_name") or info.get("filename") or "video"
    size_val = info.get("file_size") or info.get("size") or info.get("filesize") or 0

    # Try converting to human-readable
    try:
        size_str = human_readable_size(int(size_val))
    except:
        size_str = str(size_val)

    return jsonify({
        "name": filename,
        "size": size_str
    })

@app.route("/download")
def download_video():
    share_url = request.args.get("share_url", "").strip()
    if not share_url:
        return "Missing `share_url` parameter", 400

    cookie_str = load_tb_cookie_string()
    tb = TeraboxDL(cookie_str)
    info = tb.get_file_info(share_url)
    if info.get("error"):
        return f"Error: {info['error']}", 500

    dl_link = info.get("download_link") or info.get("downloadLink")
    if not dl_link:
        return "Error: no download link found", 500

    # Get the same headers as stream, minus Range
    upstream_headers = {}
    if cookie_str:
        upstream_headers["Cookie"] = cookie_str

    upstream = make_retry_session().get(
        dl_link, headers=upstream_headers, stream=True, timeout=10
    )

    def generate():
        for chunk in upstream.iter_content(chunk_size=8192):
            if chunk:
                yield chunk

    excluded = {"Transfer-Encoding", "Connection", "Content-Encoding"}
    headers = {k: v for k, v in upstream.headers.items() if k not in excluded}

    # Force download with the correct filename
    filename = info.get("file_name") or info.get("filename") or "video"
    headers["Content-Disposition"] = f'attachment; filename="{filename}"'

    return Response(generate(), status=upstream.status_code, headers=headers)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 4000))
    app.run(host="0.0.0.0", port=port, threaded=True)
