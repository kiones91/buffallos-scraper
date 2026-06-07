from flask import (
    Flask, render_template, request, send_file, Response, jsonify,
    session, redirect, url_for,
)
import os
import re
import shutil
import uuid
import queue
import threading
import time
import gc
import hashlib
import secrets as _secrets
from datetime import timedelta
from functools import wraps
from werkzeug.security import generate_password_hash, check_password_hash
from downloader import WebsiteDownloader, zip_directory, get_site_name
import user_store
import scrape_store
import mailer

app = Flask(__name__)

# --- Authentication ------------------------------------------------------
# Contas de usuario com senha propria, persistidas via user_store (HF Dataset).
# Configure no Hugging Face (Settings -> Variables and secrets):
#   SECRET_KEY     -> chave para assinar a sessao (use SECRET, nao Variable)
#   ADMIN_EMAILS   -> e-mails de admin (separados por virgula); sempre autorizados
#   ALLOWED_EMAILS -> e-mails autorizados a se cadastrar (alem dos do painel admin)
# Persistencia (escolha UM backend duravel):
#   SUPABASE_URL + SUPABASE_KEY  -> Postgres do Supabase (preferido; SECRET a key)
#   USERS_REPO + HF_TOKEN        -> dataset privado HF (fallback)
app.secret_key = os.environ.get('SECRET_KEY') or os.urandom(32)
app.permanent_session_lifetime = timedelta(days=7)

EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')

# Endpoints da API que devem responder 401 (em vez de redirecionar) sem login.
_API_PREFIXES = ('/start-download', '/stream', '/download-file')


def _env_emails(var):
    raw = os.environ.get(var, '')
    return {e.strip().lower() for e in raw.split(',') if e.strip()}


def admin_emails():
    emails = _env_emails('ADMIN_EMAILS')
    sa = os.environ.get('SUPERADMIN_EMAIL', '').strip().lower()
    if sa:
        emails.add(sa)
    return emails


def bootstrap_superadmin():
    """Cria a conta de superadmin a partir das variaveis de ambiente, caso
    ainda nao exista. Nao sobrescreve a senha se a conta ja existir (assim a
    troca feita pelo usuario persiste)."""
    email = os.environ.get('SUPERADMIN_EMAIL', '').strip().lower()
    password = os.environ.get('SUPERADMIN_PASSWORD', '')
    if not email or not password:
        return
    try:
        if not user_store.get_user(email):
            user_store.upsert_user(
                email,
                generate_password_hash(password),
                role='admin',
                name=os.environ.get('SUPERADMIN_NAME', 'Super Admin'),
            )
            print(f"[bootstrap] superadmin criado: {email}")
    except Exception as exc:
        print(f"[bootstrap] falha ao criar superadmin: {exc}")


def is_admin(email):
    return bool(email) and email.lower() in admin_emails()


def can_register(email):
    """Quem pode criar conta: admins, e-mails do ALLOWED_EMAILS (env) ou da
    allowlist gerenciada pelo admin no painel."""
    email = (email or '').lower()
    return (
        email in admin_emails()
        or email in _env_emails('ALLOWED_EMAILS')
        or user_store.is_allowed(email)
    )


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get('user_email'):
            if request.path.startswith(_API_PREFIXES):
                return jsonify({'error': 'auth_required'}), 401
            return redirect(url_for('login'))
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        email = session.get('user_email')
        if not email:
            return redirect(url_for('login'))
        if not is_admin(email):
            return redirect(url_for('index'))
        return view(*args, **kwargs)
    return wrapped

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

bootstrap_superadmin()


def _display_name(email):
    user = user_store.get_user(email) or {}
    name = user.get('name')
    if name:
        return name
    return (email or '').split('@')[0]


def _start_session(email):
    session['user_email'] = email
    session['user_name'] = _display_name(email)
    session['is_admin'] = is_admin(email)
    session.permanent = True


@app.route('/login', methods=['GET', 'POST'])
def login():
    if session.get('user_email'):
        return redirect(url_for('index'))

    error = None
    if request.method == 'POST':
        email = (request.form.get('email') or '').strip().lower()
        password = request.form.get('password') or ''

        # Fallback robusto: o superadmin SEMPRE entra com as credenciais do
        # ambiente, mesmo que a persistencia (Supabase/Dataset) esteja indisponivel.
        sa_email = os.environ.get('SUPERADMIN_EMAIL', '').strip().lower()
        sa_pass = os.environ.get('SUPERADMIN_PASSWORD', '')
        if sa_email and sa_pass and email == sa_email and password == sa_pass:
            try:
                if not user_store.get_user(email):
                    user_store.upsert_user(
                        email, generate_password_hash(password), role='admin',
                        name=os.environ.get('SUPERADMIN_NAME', 'Super Admin'),
                    )
            except Exception as exc:
                print(f"[login] superadmin upsert falhou (seguindo mesmo assim): {exc}")
            _start_session(email)
            return redirect(url_for('index'))

        user = user_store.get_user(email)
        if not EMAIL_RE.match(email) or not password:
            error = 'Informe e-mail e senha.'
        elif not user:
            error = 'Conta não encontrada. Crie sua senha em "Criar conta".'
        elif not user.get('active', True):
            error = 'Esta conta está desativada. Fale com o administrador.'
        elif not check_password_hash(user.get('password_hash', ''), password):
            error = 'E-mail ou senha incorretos.'
        else:
            _start_session(email)
            return redirect(url_for('index'))

    return render_template('login.html', error=error)


@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if session.get('user_email'):
        return redirect(url_for('index'))

    error = None
    if request.method == 'POST':
        name = (request.form.get('name') or '').strip()
        email = (request.form.get('email') or '').strip().lower()
        password = request.form.get('password') or ''
        confirm = request.form.get('confirm') or ''

        if not EMAIL_RE.match(email):
            error = 'Digite um e-mail válido.'
        elif not can_register(email):
            error = 'Este e-mail não está autorizado. Peça ao administrador para liberar seu acesso.'
        elif user_store.get_user(email):
            error = 'Já existe uma conta com este e-mail. Faça login.'
        elif len(password) < 6:
            error = 'A senha precisa ter pelo menos 6 caracteres.'
        elif password != confirm:
            error = 'As senhas não conferem.'
        else:
            role = 'admin' if is_admin(email) else 'user'
            user_store.upsert_user(
                email, generate_password_hash(password), role=role, name=name or None
            )
            _start_session(email)
            return redirect(url_for('index'))

    return render_template('signup.html', error=error)


def _base_url():
    base = os.environ.get('APP_BASE_URL', '').strip().rstrip('/')
    if base:
        return base
    return request.url_root.rstrip('/')


def _send_reset_email(email):
    token = _secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    expires = time.time() + 3600  # 1 hora
    user_store.set_reset(email, token_hash, expires)

    link = f"{_base_url()}/reset?email={email}&token={token}"
    html = f"""
        <div style="font-family:Arial,sans-serif;max-width:480px;margin:auto">
          <h2>Buffallos · Redefinir senha</h2>
          <p>Recebemos um pedido para redefinir a senha desta conta.</p>
          <p><a href="{link}" style="background:#4a6cf7;color:#fff;padding:12px 20px;
             border-radius:8px;text-decoration:none;display:inline-block">Redefinir minha senha</a></p>
          <p style="color:#666;font-size:13px">Ou copie e cole este link (válido por 1 hora):<br>{link}</p>
          <p style="color:#999;font-size:12px">Se você não pediu isso, ignore este e-mail.</p>
        </div>
    """
    return mailer.send_email(email, "Buffallos · Redefinir sua senha", html)


@app.route('/forgot', methods=['GET', 'POST'])
def forgot():
    message = None
    error = None
    if request.method == 'POST':
        email = (request.form.get('email') or '').strip().lower()
        user = user_store.get_user(email)
        if user:
            if mailer.enabled():
                _send_reset_email(email)
            else:
                user_store.set_pending_reset(email, True)
        # Resposta neutra para nao revelar quais e-mails existem.
        if mailer.enabled():
            message = ('Se houver uma conta com este e-mail, enviamos um link '
                       'para redefinir a senha. Verifique sua caixa de entrada e spam.')
        else:
            message = ('Se houver uma conta com este e-mail, o administrador foi '
                       'avisado e vai redefinir sua senha em breve.')

    return render_template('forgot.html', message=message, error=error)


@app.route('/reset', methods=['GET', 'POST'])
def reset():
    email = (request.values.get('email') or '').strip().lower()
    token = request.values.get('token') or ''
    error = None

    user = user_store.get_user(email)
    token_hash = hashlib.sha256(token.encode()).hexdigest() if token else ''
    valid = bool(
        user
        and token
        and user.get('reset_token')
        and _secrets.compare_digest(user.get('reset_token', ''), token_hash)
        and (user.get('reset_expires', 0) or 0) > time.time()
    )

    if request.method == 'POST':
        if not valid:
            error = 'Link inválido ou expirado. Solicite um novo.'
        else:
            new = request.form.get('new') or ''
            confirm = request.form.get('confirm') or ''
            if len(new) < 6:
                error = 'A nova senha precisa ter pelo menos 6 caracteres.'
            elif new != confirm:
                error = 'As senhas não conferem.'
            else:
                user_store.set_password(email, generate_password_hash(new))
                return render_template('reset.html', done=True)

    return render_template(
        'reset.html', email=email, token=token, valid=valid, error=error, done=False
    )


@app.route('/account', methods=['GET', 'POST'])
@login_required
def account():
    email = session.get('user_email')
    user = user_store.get_user(email)
    message = None
    error = None

    if request.method == 'POST':
        current = request.form.get('current') or ''
        new = request.form.get('new') or ''
        confirm = request.form.get('confirm') or ''

        if not user or not check_password_hash(user.get('password_hash', ''), current):
            error = 'Senha atual incorreta.'
        elif len(new) < 6:
            error = 'A nova senha precisa ter pelo menos 6 caracteres.'
        elif new != confirm:
            error = 'As senhas não conferem.'
        else:
            user_store.set_password(email, generate_password_hash(new))
            message = 'Senha atualizada com sucesso.'

    return render_template('account.html', user_email=email, message=message, error=error)


@app.route('/admin')
@admin_required
def admin():
    users = user_store.list_users()
    allowlist = sorted(user_store.get_allowlist())
    temp_password = session.pop('temp_password', None)
    temp_for = session.pop('temp_for', None)
    return render_template(
        'admin.html',
        user_email=session.get('user_email'),
        users=users,
        allowlist=allowlist,
        admin_emails=sorted(admin_emails()),
        temp_password=temp_password,
        temp_for=temp_for,
        hub_ok=user_store.is_persistent(),
        backend=user_store.backend_name(),
    )


@app.route('/admin/allow', methods=['POST'])
@admin_required
def admin_allow():
    email = (request.form.get('email') or '').strip().lower()
    if EMAIL_RE.match(email):
        user_store.add_allowed(email)
    return redirect(url_for('admin'))


@app.route('/admin/remove-allow', methods=['POST'])
@admin_required
def admin_remove_allow():
    email = (request.form.get('email') or '').strip().lower()
    user_store.remove_allowed(email)
    return redirect(url_for('admin'))


@app.route('/admin/reset', methods=['POST'])
@admin_required
def admin_reset():
    email = (request.form.get('email') or '').strip().lower()
    if user_store.get_user(email):
        temp = _secrets.token_urlsafe(6)
        user_store.set_password(email, generate_password_hash(temp))
        # Mostrado uma unica vez ao admin para repassar ao usuario.
        session['temp_password'] = temp
        session['temp_for'] = email
    return redirect(url_for('admin'))


@app.route('/admin/toggle', methods=['POST'])
@admin_required
def admin_toggle():
    email = (request.form.get('email') or '').strip().lower()
    user = user_store.get_user(email)
    if user:
        user_store.set_active(email, not user.get('active', True))
    return redirect(url_for('admin'))


@app.route('/admin/delete', methods=['POST'])
@admin_required
def admin_delete():
    email = (request.form.get('email') or '').strip().lower()
    if email != session.get('user_email'):
        user_store.delete_user(email)
    return redirect(url_for('admin'))


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


@app.route('/')
@login_required
def index():
    return render_template(
        'index.html',
        user_email=session.get('user_email'),
        user_name=session.get('user_name') or _display_name(session.get('user_email')),
        is_admin=session.get('is_admin', False),
        library_enabled=scrape_store.enabled(),
    )


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
@login_required
def start_download():
    """Start download process and return session ID for SSE."""
    data = request.get_json(silent=True) or {}
    url = data.get('url')

    if not url:
        return jsonify({'error': 'URL is required'}), 400

    session_id = str(uuid.uuid4())
    now = time.time()
    user_email = session.get('user_email')

    with session_lock:
        message_queues[session_id] = queue.Queue()
        download_results[session_id] = {
            'status': 'processing',
            'zip_path': None,
            'filename': None,
            'started_at': now,
        }

    thread = threading.Thread(target=process_download, args=(session_id, url, user_email))
    thread.daemon = True
    thread.start()

    return jsonify({'session_id': session_id})


def process_download(session_id, url, user_email=None):
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

        saved_to_library = False
        if scrape_store.enabled() and user_email:
            try:
                q.put("☁️ Salvando na sua biblioteca (Supabase)...")
                scrape_store.add_scrape(user_email, url, site_name, zip_path)
                saved_to_library = True
                # ZIP agora vive no Supabase; libera o disco efemero do Space.
                if os.path.exists(zip_path):
                    os.remove(zip_path)
            except Exception as up_err:
                q.put(f"⚠️ Não consegui salvar na biblioteca: {up_err}. "
                      f"Você ainda pode baixar agora.")

        q.put("🎉 Pronto!")
        with session_lock:
            download_results[session_id] = {
                'status': 'complete',
                'zip_path': None if saved_to_library else zip_path,
                'filename': zip_filename,
                'saved': saved_to_library,
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
@login_required
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
@login_required
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


@app.route('/api/scrapes')
@login_required
def api_scrapes():
    """List the logged-in user's saved scrapes (JSON)."""
    if not scrape_store.enabled():
        return jsonify({'enabled': False, 'items': []})
    try:
        items = scrape_store.list_scrapes(session.get('user_email'))
        return jsonify({'enabled': True, 'items': items})
    except Exception as e:
        return jsonify({'enabled': True, 'items': [], 'error': str(e)}), 500


@app.route('/scrape/<scrape_id>/download')
@login_required
def scrape_download(scrape_id):
    """Stream a saved ZIP from Supabase Storage to the user."""
    if not scrape_store.enabled():
        return "Biblioteca indisponível", 404
    row = scrape_store.get_scrape(scrape_id, session.get('user_email'))
    if not row:
        return "Não encontrado", 404
    try:
        data = scrape_store.download_bytes(row['storage_path'])
    except Exception as e:
        print(f"❌ Erro ao baixar do Supabase: {e}")
        return "Erro ao baixar arquivo", 500

    filename = f"{row.get('site_name') or 'site'}.zip"
    return Response(
        data,
        mimetype='application/zip',
        headers={
            'Content-Disposition': f'attachment; filename="{filename}"',
            'Content-Length': str(len(data)),
        },
    )


@app.route('/scrape/<scrape_id>/delete', methods=['POST'])
@login_required
def scrape_delete(scrape_id):
    if not scrape_store.enabled():
        return jsonify({'error': 'unavailable'}), 404
    try:
        ok = scrape_store.delete_scrape(scrape_id, session.get('user_email'))
        return jsonify({'ok': bool(ok)})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


if __name__ == '__main__':
    app.run(debug=True, port=5001, threaded=True)
else:
    # Production: Gunicorn entrypoint
    pass
