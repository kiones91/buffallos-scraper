from flask import Flask, render_template, request, send_file, Response, jsonify
import os
import shutil
import uuid
import queue
import threading
import time
import gc
from downloader import WebsiteDownloader, zip_directory, get_site_name

app = Flask(__name__)

DOWNLOAD_FOLDER = 'downloads'
if not os.path.exists(DOWNLOAD_FOLDER):
    os.makedirs(DOWNLOAD_FOLDER)

# Tunable retention windows (seconds)
COMPLETE_TTL = 1800       # complete sessions (zip waiting for download)
ERROR_TTL = 600           # error sessions
PROCESSING_TTL = 1800     # safety net for stuck/zombie sessions
ORPHAN_FILE_TTL = 1800    # files on disk with no matching session
CLEANUP_INTERVAL = 300    # how often the janitor runs


def cleanup_downloads_folder():
    """Remove all files and folders from downloads directory."""
    try:
        for item in os.listdir(DOWNLOAD_FOLDER):
            item_path = os.path.join(DOWNLOAD_FOLDER, item)
            if os.path.isfile(item_path):
                os.remove(item_path)
            elif os.path.isdir(item_path):
                shutil.rmtree(item_path)
        print("🧹 Pasta downloads limpa com sucesso")
    except Exception as e:
        print(f"⚠️ Erro ao limpar pasta downloads: {e}")


cleanup_downloads_folder()

# Per-session state. Always touch via session_lock when iterating/mutating.
message_queues = {}
download_results = {}
session_lock = threading.Lock()


def _purge_session(session_id):
    """Remove a single session's in-memory state and any disk artifacts."""
    with session_lock:
        result = download_results.pop(session_id, None)
        message_queues.pop(session_id, None)

    if not result:
        return

    zip_path = result.get('zip_path')
    if zip_path and os.path.exists(zip_path):
        try:
            os.remove(zip_path)
        except Exception:
            pass

    # Some error paths may leave the raw directory behind.
    raw_dir = os.path.join(DOWNLOAD_FOLDER, session_id)
    if os.path.isdir(raw_dir):
        try:
            shutil.rmtree(raw_dir)
        except Exception:
            pass


def _cleanup_orphan_files():
    """
    Remove files/dirs in downloads/ that don't belong to any active session.
    Catches leftovers from worker crashes or restarts.
    """
    try:
        with session_lock:
            known_ids = set(download_results.keys())

        now = time.time()
        for entry in os.listdir(DOWNLOAD_FOLDER):
            path = os.path.join(DOWNLOAD_FOLDER, entry)
            try:
                age = now - os.path.getmtime(path)
            except OSError:
                continue

            # Strip trailing .zip to recover the session uuid
            base = entry[:-4] if entry.endswith('.zip') else entry
            if base in known_ids:
                continue
            if age < ORPHAN_FILE_TTL:
                continue

            try:
                if os.path.isfile(path):
                    os.remove(path)
                    print(f"🗑️ Removido arquivo órfão: {entry}")
                elif os.path.isdir(path):
                    shutil.rmtree(path)
                    print(f"🗑️ Removido diretório órfão: {entry}")
            except Exception:
                pass
    except Exception as e:
        print(f"⚠️ Erro varrendo órfãos: {e}")


def cleanup_abandoned_sessions():
    """
    Janitor thread: removes complete/error/zombie sessions and orphan files.
    Runs every CLEANUP_INTERVAL seconds.
    """
    while True:
        time.sleep(CLEANUP_INTERVAL)
        try:
            now = time.time()
            to_remove = []

            with session_lock:
                snapshot = list(download_results.items())

            for session_id, result in snapshot:
                status = result.get('status')
                created_at = result.get('created_at') or result.get('started_at') or 0
                if not created_at:
                    continue
                age = now - created_at

                if status == 'complete' and age > COMPLETE_TTL:
                    to_remove.append((session_id, 'complete'))
                elif status == 'error' and age > ERROR_TTL:
                    to_remove.append((session_id, 'error'))
                elif status == 'processing' and age > PROCESSING_TTL:
                    to_remove.append((session_id, 'zombie'))

            for session_id, reason in to_remove:
                _purge_session(session_id)
                print(f"🧹 Sessão {session_id[:8]} removida ({reason})")

            _cleanup_orphan_files()
            gc.collect()
        except Exception as e:
            print(f"⚠️ Erro no janitor: {e}")


threading.Thread(target=cleanup_abandoned_sessions, daemon=True).start()


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/health')
def health():
    """Lightweight health endpoint with memory + session counts for monitoring."""
    info = {'status': 'ok'}
    with session_lock:
        info['sessions'] = len(download_results)
        info['queues'] = len(message_queues)

    try:
        import psutil
        proc = psutil.Process()
        info['rss_mb'] = round(proc.memory_info().rss / (1024 * 1024), 1)
    except Exception:
        # psutil is optional - fall back to resource module on POSIX
        try:
            import resource
            rss_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            # macOS reports bytes, Linux reports kilobytes
            divisor = 1024 * 1024 if os.uname().sysname == 'Darwin' else 1024
            info['rss_mb'] = round(rss_kb / divisor, 1)
        except Exception:
            pass

    return jsonify(info)


@app.route('/start-download', methods=['POST'])
def start_download():
    """Start download process and return session ID for SSE."""
    data = request.get_json(silent=True) or {}
    url = data.get('url')

    if not url:
        return jsonify({'error': 'URL is required'}), 400

    session_id = str(uuid.uuid4())
    now = time.time()

    with session_lock:
        message_queues[session_id] = queue.Queue()
        download_results[session_id] = {
            'status': 'processing',
            'zip_path': None,
            'filename': None,
            'started_at': now,
        }

    thread = threading.Thread(target=process_download, args=(session_id, url))
    thread.daemon = True
    thread.start()

    return jsonify({'session_id': session_id})


def process_download(session_id, url):
    """Background download worker."""
    with session_lock:
        q = message_queues.get(session_id)
    if q is None:
        return

    download_dir = os.path.join(DOWNLOAD_FOLDER, session_id)
    zip_path = os.path.join(DOWNLOAD_FOLDER, f"{session_id}.zip")

    def log_callback(message):
        q.put(message)

    downloader = None
    try:
        downloader = WebsiteDownloader(url, download_dir, log_callback=log_callback)
        success = downloader.process()

        if not success:
            q.put("❌ Falha no download")
            with session_lock:
                download_results[session_id] = {
                    'status': 'error',
                    'error': 'Failed to download site',
                    'created_at': time.time(),
                }
            return

        site_name = get_site_name(url)
        zip_filename = f"{site_name}.zip"

        q.put("📦 Criando arquivo ZIP...")
        zip_directory(download_dir, zip_path)

        # Free raw files immediately
        if os.path.isdir(download_dir):
            shutil.rmtree(download_dir, ignore_errors=True)

        q.put("🎉 Download pronto!")
        with session_lock:
            download_results[session_id] = {
                'status': 'complete',
                'zip_path': zip_path,
                'filename': zip_filename,
                'created_at': time.time(),
            }

    except Exception as e:
        q.put(f"❌ Erro: {str(e)}")
        with session_lock:
            download_results[session_id] = {
                'status': 'error',
                'error': str(e),
                'created_at': time.time(),
            }
        # Best-effort cleanup of partial artifacts
        if os.path.exists(download_dir):
            shutil.rmtree(download_dir, ignore_errors=True)
        if os.path.exists(zip_path):
            try:
                os.remove(zip_path)
            except Exception:
                pass

    finally:
        # Drop downloader reference so its in-memory buffers can be GC'd
        downloader = None
        gc.collect()


@app.route('/stream/<session_id>')
def stream(session_id):
    """SSE endpoint for log streaming."""
    def generate():
        with session_lock:
            q = message_queues.get(session_id)

        if q is None:
            yield "data: ❌ Sessão não encontrada\n\n"
            yield "event: done\ndata: error\n\n"
            return

        # Hard cap how long a single SSE connection can live to avoid
        # accumulating zombie generators.
        deadline = time.time() + 30 * 60  # 30 minutes

        while True:
            if time.time() > deadline:
                yield "data: ⏱️ Conexão encerrada por inatividade\n\n"
                yield "event: done\ndata: timeout\n\n"
                return

            try:
                message = q.get(timeout=30)
                yield f"data: {message}\n\n"

                with session_lock:
                    result = download_results.get(session_id, {})
                if result.get('status') in ('complete', 'error'):
                    yield f"event: done\ndata: {result['status']}\n\n"
                    return

            except queue.Empty:
                with session_lock:
                    result = download_results.get(session_id, {})
                # Worker died/finished without final message - don't hang forever
                if result.get('status') in ('complete', 'error'):
                    yield f"event: done\ndata: {result['status']}\n\n"
                    return
                yield ": keepalive\n\n"

    return Response(generate(), mimetype='text/event-stream')


@app.route('/download-file/<session_id>')
def download_file(session_id):
    """Download the generated ZIP file and clean up immediately."""
    with session_lock:
        result = download_results.get(session_id)

    if not result or result.get('status') != 'complete':
        return "File not ready", 404

    zip_path = result['zip_path']
    filename = result['filename']

    if not zip_path or not os.path.exists(zip_path):
        # File was already cleaned up - drop the stale session entry
        _purge_session(session_id)
        return "File not found", 404

    try:
        response = send_file(zip_path, as_attachment=True, download_name=filename)

        def cleanup():
            time.sleep(2)
            _purge_session(session_id)
            print(f"🗑️ Sessão {session_id[:8]} ({filename}) removida após download")

        threading.Thread(target=cleanup, daemon=True).start()
        return response
    except Exception as e:
        print(f"❌ Erro ao enviar arquivo: {e}")
        return "Error sending file", 500


if __name__ == '__main__':
    app.run(debug=True, port=5001, threaded=True)
else:
    # Production: Gunicorn entrypoint
    pass
