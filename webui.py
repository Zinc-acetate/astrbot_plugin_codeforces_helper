import asyncio
import secrets
from datetime import timedelta
from quart import Quart, send_from_directory
from hypercorn.config import Config
import hypercorn.asyncio
from pathlib import Path

from .backend.api import api

app = Quart(__name__, static_folder='public', template_folder='public')
app.register_blueprint(api, url_prefix="/api")

@app.route('/')
async def index():
    public_dir = Path(__file__).parent / 'public'
    return await send_from_directory(public_dir, 'index.html')

@app.route('/<path:filename>')
async def serve_static(filename):
    public_dir = Path(__file__).parent / 'public'
    return await send_from_directory(public_dir, filename)

async def start_server(db_path, port, plugin_config):
    app.config['DB_PATH'] = db_path
    app.config['PLUGIN_CONFIG'] = plugin_config
    # 每次 WebUI 进程启动生成新的会话密钥，避免将密钥硬编码在仓库中。
    app.secret_key = secrets.token_hex(32)
    app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=12)
    app.config['SESSION_COOKIE_HTTPONLY'] = True
    app.config['SESSION_COOKIE_SAMESITE'] = 'Strict'

    hypercorn_config = Config()
    hypercorn_config.bind = [f"0.0.0.0:{port}"]
    print(f"[Codeforces Helper WebUI] Server is running on http://0.0.0.0:{port}")
    await hypercorn.asyncio.serve(app, hypercorn_config)

def run_server(db_path, port, plugin_config):
    print(f"[Codeforces Helper WebUI] Process started. DB path: {db_path}, Port: {port}")
    try:
        asyncio.run(start_server(db_path, port, plugin_config))
    except KeyboardInterrupt:
        print("[Codeforces Helper WebUI] Server stopped by KeyboardInterrupt.")
    except Exception as e:
        print(f"[Codeforces Helper WebUI] An error occurred: {e}")
