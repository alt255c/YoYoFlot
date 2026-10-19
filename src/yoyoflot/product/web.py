"""YoYoFlot Sentinel local web application.

The server reads a verified product snapshot. The optional upload action only
accepts another prepared product ZIP; it never runs arbitrary code or rebuilds
the model from an untrusted browser request. ``--demo`` explicitly activates
a small, real subset of the active source-dataset product for presentations.
"""
import argparse
import io
import json
import os
import re
import shutil
import tempfile
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .demo import DemoSubsetStore
from .store import ProductStore


MAX_UPLOAD_BYTES = 512 * 1024 * 1024
MAX_UNPACKED_BYTES = 2 * 1024 * 1024 * 1024
MAX_ZIP_MEMBERS = 20_000


class UploadError(ValueError):
    """A user-facing, safe upload validation failure."""


class StoreManager:
    def __init__(self, store):
        self._lock = threading.RLock()
        self._initial = store
        self._base = store
        self._store = store
        self._temporary_roots = []

    def current(self):
        with self._lock:
            return self._store

    def replace(self, store, temporary_root=None, as_base=False):
        with self._lock:
            self._store = store
            if as_base:
                self._base = store
            if temporary_root:
                self._temporary_roots.append(Path(temporary_root))

    def reset(self):
        with self._lock:
            self._store = self._base

    def demo(self):
        with self._lock:
            self._store = DemoSubsetStore(self._base)


def _int_param(query, name, default, minimum, maximum):
    value=query.get(name, [str(default)])
    if len(value) != 1:
        raise ValueError(f'Expected one {name} parameter')
    try:
        parsed=int(value[0])
    except ValueError as exc:
        raise ValueError(f'{name} must be an integer') from exc
    if not minimum <= parsed <= maximum:
        raise ValueError(f'{name} must be between {minimum} and {maximum}')
    return parsed


def _multipart_file(headers, body):
    content_type=headers.get('Content-Type','')
    match=re.search(r'boundary=(?:"([^"]+)"|([^;]+))', content_type, re.I)
    if not content_type.lower().startswith('multipart/form-data') or not match:
        raise UploadError('Ожидается multipart/form-data с ZIP-файлом продукта')
    boundary=(match.group(1) or match.group(2)).encode('utf-8')
    marker=b'--'+boundary
    for part in body.split(marker):
        if b'filename=' not in part[:1500]:
            continue
        header_end=part.find(b'\r\n\r\n')
        if header_end < 0:
            continue
        header=part[:header_end].decode('utf-8','replace')
        filename_match=re.search(r'filename="([^"]*)"', header, re.I)
        filename=(filename_match.group(1) if filename_match else 'upload.zip').strip()
        payload=part[header_end+4:]
        if payload.endswith(b'\r\n'):
            payload=payload[:-2]
        if not payload:
            raise UploadError('Загруженный файл пуст')
        return filename, payload
    raise UploadError('Файл продукта не найден в запросе')


def _extract_product(payload):
    if len(payload) > MAX_UPLOAD_BYTES:
        raise UploadError(f'Файл слишком большой: максимум {MAX_UPLOAD_BYTES // 1024 // 1024} МБ')
    temp_root=Path(tempfile.mkdtemp(prefix='yoyoflot-upload-'))
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            infos=archive.infolist()
            if len(infos) > MAX_ZIP_MEMBERS:
                raise UploadError('В ZIP слишком много файлов')
            total=0
            for info in infos:
                name=Path(info.filename)
                if name.is_absolute() or '..' in name.parts:
                    raise UploadError('ZIP содержит небезопасный путь')
                if info.is_dir():
                    continue
                total += info.file_size
                if total > MAX_UNPACKED_BYTES:
                    raise UploadError('Распакованный продукт слишком большой')
                target=(temp_root/name).resolve()
                if temp_root not in target.parents:
                    raise UploadError('ZIP выходит за пределы временного каталога')
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.read(info))
        candidates=list(temp_root.rglob('run_manifest.json'))
        if len(candidates) != 1:
            raise UploadError('В ZIP должен быть ровно один run_manifest.json')
        return candidates[0].parent, temp_root
    except (zipfile.BadZipFile, OSError) as exc:
        shutil.rmtree(temp_root, ignore_errors=True)
        raise UploadError(f'Не удалось прочитать ZIP: {exc}') from exc
    except Exception:
        shutil.rmtree(temp_root, ignore_errors=True)
        raise


def handler(manager):
    class Handler(BaseHTTPRequestHandler):
        server_version='YoYoFlotSentinel/1.0'

        def do_GET(self):
            path=urlsplit(self.path)
            try:
                query=parse_qs(path.query, keep_blank_values=True)
                store=manager.current()
                if path.path == '/api/metadata':
                    payload=store.metadata()
                elif path.path == '/api/top':
                    payload=store.top(_int_param(query,'limit',50,1,1000))
                elif path.path == '/api/users':
                    payload=store.users(
                        limit=_int_param(query,'limit',50,1,1000),
                        offset=_int_param(query,'offset',0,0,10_000_000),
                        query=query.get('q',[''])[0],
                        eligibility=query.get('eligibility',['all'])[0],
                        sort=query.get('sort',['rank'])[0],
                    )
                elif path.path == '/api/profile':
                    if set(query) != {'id'} or len(query['id']) != 1:
                        raise ValueError('Expected one id parameter')
                    payload=store.profile(query['id'][0])
                elif path.path in {'/','/app.js','/style.css','/world.geojson','/favicon.svg','/mosquito.gif'}:
                    name='index.html' if path.path=='/' else path.path[1:]
                    content=files('yoyoflot.product').joinpath('static',name).read_bytes()
                    mime={'index.html':'text/html; charset=utf-8','app.js':'text/javascript; charset=utf-8',
                          'style.css':'text/css; charset=utf-8','world.geojson':'application/geo+json',
                          'favicon.svg':'image/svg+xml','mosquito.gif':'image/gif'}[name]
                    self.send_content(200,content,mime)
                    return
                else:
                    self.send_json(404,{'error':'Not found'})
                    return
                self.send_json(200,payload)
            except KeyError:
                self.send_json(404,{'error':'Профиль не найден'})
            except ValueError as exc:
                self.send_json(400,{'error':str(exc)})
            except Exception:
                self.log_error('Failed request: %s',path.path)
                self.send_json(500,{'error':'Ошибка чтения снимка продукта'})

        def do_POST(self):
            path=urlsplit(self.path).path
            try:
                if path == '/api/demo':
                    manager.demo()
                    self.send_json(200,{'ok':True,'mode':'demo_subset','metadata':manager.current().metadata()})
                    return
                if path == '/api/reset':
                    manager.reset()
                    self.send_json(200,{'ok':True,'mode':manager.current().data_mode,'metadata':manager.current().metadata()})
                    return
                if path != '/api/upload':
                    self.send_json(404,{'error':'Not found'})
                    return
                try:
                    length=int(self.headers.get('Content-Length','0'))
                except ValueError as exc:
                    raise UploadError('Некорректный Content-Length') from exc
                if length <= 0 or length > MAX_UPLOAD_BYTES + 1024 * 1024:
                    raise UploadError(f'Размер запроса должен быть от 1 байта до {MAX_UPLOAD_BYTES // 1024 // 1024} МБ')
                filename,payload=_multipart_file(self.headers,self.rfile.read(length))
                if not filename.lower().endswith('.zip'):
                    raise UploadError('Загрузить можно только ZIP подготовленного продукта')
                product_root,temp_root=_extract_product(payload)
                try:
                    product=ProductStore(product_root)
                except Exception as exc:
                    shutil.rmtree(temp_root, ignore_errors=True)
                    raise UploadError(f'ZIP не является проверенным продуктом: {exc}') from exc
                manager.replace(product,temp_root,as_base=True)
                self.send_json(200,{'ok':True,'mode':product.data_mode,'metadata':product.metadata()})
            except UploadError as exc:
                self.send_json(413 if 'слишком' in str(exc) else 400,{'error':str(exc)})
            except Exception:
                self.log_error('Failed POST request: %s',path)
                self.send_json(500,{'error':'Не удалось активировать продукт'})

        def send_json(self,status,payload):
            self.send_content(status,json.dumps(payload,ensure_ascii=False,allow_nan=False,default=str).encode(),'application/json; charset=utf-8')

        def send_content(self,status,content,mime):
            self.send_response(status)
            self.send_header('Content-Type',mime)
            self.send_header('Content-Length',str(len(content)))
            self.send_header('Cache-Control','no-store' if mime.startswith('application/json') else 'no-cache')
            self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers()
            self.wfile.write(content)

        def log_message(self, format, *args):
            super().log_message(format, *args)

    return Handler


def serve(product=None, host='127.0.0.1', port=8501, demo=False):
    source=ProductStore(product)
    manager=StoreManager(source)
    if demo:
        manager.demo()
    initial=manager.current()
    with ThreadingHTTPServer((host,port),handler(manager)) as server:
        print(f'YoYoFlot Sentinel: {initial.metadata()["run_id"]}; HTTP port {server.server_port}; mode={initial.data_mode}',flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--product',default=os.environ.get('YOYO_PRODUCT'))
    p.add_argument('--demo',action='store_true',default=os.environ.get('YOYO_DEMO','').lower() in {'1','true','yes'})
    p.add_argument('--host',default=os.environ.get('YOYO_HOST','127.0.0.1'))
    p.add_argument('--port',type=int,default=int(os.environ.get('YOYO_PORT','8501')))
    a=p.parse_args(argv)
    if not a.product:
        p.error('--product is required; --demo selects a real subset from that product')
    serve(a.product,a.host,a.port,a.demo)


if __name__=='__main__':
    main()
