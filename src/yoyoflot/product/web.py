"""Read-only published scoring, isolated browser views and bounded ZIP upload."""
import argparse
from contextlib import contextmanager
import hmac
import io
import json
import os
import secrets
import shutil
import tempfile
import threading
import time
import zipfile
from importlib.resources import files
from pathlib import Path
from urllib.parse import urlsplit

from flask import Flask, Response, jsonify, request, session
from werkzeug.exceptions import HTTPException
from .demo import DemoSubsetStore
from .store import ProductStore
from .scoring import default_path as default_scoring_path, load as load_scoring

MAX_UPLOAD_BYTES = 512 * 1024 * 1024
MAX_UNPACKED_BYTES = 2 * 1024 * 1024 * 1024
MAX_ZIP_MEMBERS = 20_000


class UploadError(ValueError):
    pass


class StoreManager:
    """Browser-local views; uploads are reclaimed after the final reader exits."""
    def __init__(self, store):
        self._lock = threading.RLock()
        self._initial = store
        self.scoring = getattr(store, 'scoring', None)
        self._sessions = {}
        self._readers = {}
        self._retired = []

    def _retire(self, entry):
        if entry['base'] is not self._initial:
            self._retired.append((entry['base'], entry['root']))

    def _cleanup(self):
        now = time.monotonic()
        for sid, entry in list(self._sessions.items()):
            if now-entry['seen']>3600 and not self._readers.get(id(entry['base']),0):
                self._retire(self._sessions.pop(sid))
        pending=[]
        for store,root in self._retired:
            if self._readers.get(id(store),0):
                pending.append((store,root))
            else:
                store.close()
                if root: shutil.rmtree(root,ignore_errors=True)
        self._retired=pending

    def _entry(self,sid):
        self._cleanup()
        if sid not in self._sessions:
            if len(self._sessions)>=128:
                raise ValueError('Too many active browser sessions; retry after an idle session expires')
            self._sessions[sid]=dict(base=self._initial,view=self._initial,root=None,seen=time.monotonic())
        entry=self._sessions[sid]
        entry['seen']=time.monotonic()
        return entry

    @contextmanager
    def acquire(self,sid=''):
        with self._lock:
            entry=self._entry(sid)
            store,base=entry['view'],entry['base']
            key=id(base)
            self._readers[key]=self._readers.get(key,0)+1
        try:
            yield store
        finally:
            with self._lock:
                self._readers[key]-=1
                if not self._readers[key]: del self._readers[key]
                self._cleanup()

    def current(self,sid=''):
        with self._lock:
            return self._entry(sid)['view']

    def replace(self,store,temporary_root=None,as_base=False,sid=''):
        with self._lock:
            entry=self._entry(sid)
            self._retire(entry)
            entry.update(base=store,view=store,root=temporary_root)
            self._cleanup()

    def reset(self,sid=''):
        with self._lock:
            entry=self._entry(sid)
            entry['view']=entry['base']

    def demo(self,sid=''):
        with self._lock:
            entry=self._entry(sid)
            entry['view']=DemoSubsetStore(entry['base'])

    def close(self):
        with self._lock:
            for entry in self._sessions.values(): self._retire(entry)
            self._sessions.clear()
            self._cleanup()
            self._initial.close()


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


def _extract_product(payload):
    if isinstance(payload, bytes) and len(payload) > MAX_UPLOAD_BYTES:
        raise UploadError(f'Файл слишком большой: максимум {MAX_UPLOAD_BYTES // 1024 // 1024} МБ')
    temp_root=Path(tempfile.mkdtemp(prefix='yoyoflot-upload-'))
    try:
        with zipfile.ZipFile(io.BytesIO(payload) if isinstance(payload, bytes) else payload) as archive:
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
                with archive.open(info) as source, target.open('wb') as destination:
                    shutil.copyfileobj(source,destination,length=1024*1024)
        candidates=[]
        for path in temp_root.rglob('run_manifest.json'):
            if (path.parent/'tables/rankings.parquet').is_file():
                candidate=json.loads(path.read_text(encoding='utf-8'))
                if candidate.get('data_mode')=='source_dataset' and candidate.get('status')=='complete':
                    candidates.append(path)
        if len(candidates) != 1:
            raise UploadError('В ZIP должен быть ровно один run_manifest.json')
        return candidates[0].parent, temp_root
    except (zipfile.BadZipFile, OSError) as exc:
        shutil.rmtree(temp_root, ignore_errors=True)
        raise UploadError(f'Не удалось прочитать ZIP: {exc}') from exc
    except Exception:
        shutil.rmtree(temp_root, ignore_errors=True)
        raise


def handler(manager, auth_token=None, demo=False):
    """WSGI application; served by Waitress, without a bespoke HTTP parser."""
    app=Flask(__name__,static_folder=None)
    app.secret_key=os.environ.get('YOYO_SESSION_SECRET') or secrets.token_hex(32)
    app.config.update(MAX_CONTENT_LENGTH=MAX_UPLOAD_BYTES+1024*1024,
                      SESSION_COOKIE_HTTPONLY=True,SESSION_COOKIE_SAMESITE='Lax')

    @app.before_request
    def authorize():
        if auth_token:
            auth=request.authorization
            supplied=(auth.password if auth and auth.type=='basic' else
                      request.headers.get('Authorization','').removeprefix('Bearer ')) or ''
            if not hmac.compare_digest(supplied,auth_token):
                return Response('Authentication required',401,{'WWW-Authenticate':'Basic realm="YoYoFlot"'})
        if request.method=='POST':
            origin=request.headers.get('Origin')
            if origin and urlsplit(origin).netloc != request.host:
                return jsonify(error='Cross-origin changes are forbidden'),403
        if 'view_id' not in session:
            session['view_id']=secrets.token_urlsafe(24)
            if demo: manager.demo(session['view_id'])

    @app.after_request
    def headers(response):
        response.headers['Cache-Control']='no-store' if request.path.startswith('/api/') else 'no-cache'
        response.headers['X-Content-Type-Options']='nosniff'
        response.headers['Content-Security-Policy']="default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'"
        return response

    @app.errorhandler(Exception)
    def error(exc):
        if isinstance(exc,HTTPException): return jsonify(error=exc.description),exc.code
        if isinstance(exc,KeyError): return jsonify(error='Профиль не найден'),404
        if isinstance(exc,ValueError): return jsonify(error=str(exc)),400
        app.logger.exception('Request failed')
        return jsonify(error='Ошибка чтения подготовленного продукта'),500

    @app.get('/api/<endpoint>')
    @app.get('/api/profile/source',defaults={'endpoint':'profile/source'})
    def read_api(endpoint):
        q=request.args.to_dict(flat=False)
        with manager.acquire(session['view_id']) as store:
            if endpoint=='metadata': payload=store.metadata()
            elif endpoint=='top': payload=store.top(_int_param(q,'limit',50,1,1000))
            elif endpoint=='users':
                payload=store.users(limit=_int_param(q,'limit',50,1,1000),offset=_int_param(q,'offset',0,0,10_000_000),
                    query=q.get('q',[''])[0],eligibility=q.get('eligibility',['all'])[0],sort=q.get('sort',['rank'])[0])
            elif endpoint in {'profile','profile/source'}:
                if set(q)!={'id'} or len(q['id'])!=1: raise ValueError('Expected one id parameter')
                payload=store.profile(q['id'][0]) if endpoint=='profile' else store.source_details(q['id'][0])
            elif endpoint=='marker-related':
                if not set(q)<={'id','marker','offset','limit'} or len(q.get('id',[]))!=1 or len(q.get('marker',['N01']))!=1:
                    raise ValueError('Expected id, marker and pagination parameters')
                payload=store.related_profiles(q['id'][0],q.get('marker',['N01'])[0],
                    offset=_int_param(q,'offset',0,0,10_000_000),limit=_int_param(q,'limit',10,1,50))
            else: return jsonify(error='Not found'),404
            return Response(json.dumps(payload,ensure_ascii=False,allow_nan=False,default=str),content_type='application/json; charset=utf-8')

    @app.post('/api/<endpoint>')
    def change_view(endpoint):
        sid=session['view_id']
        if endpoint=='demo': manager.demo(sid)
        elif endpoint=='reset': manager.reset(sid)
        elif endpoint=='upload':
            upload=next(iter(request.files.values()),None)
            if upload is None or not (upload.filename or '').lower().endswith('.zip'):
                raise UploadError('Загрузить можно только ZIP подготовленного продукта')
            root,temp_root=_extract_product(upload.stream)
            try:
                product=ProductStore(root)
                if product.runtime_scores is None:
                    raise UploadError('ZIP содержит промежуточный результат; соберите полный продукт через build_and_run.sh')
                manager.replace(product,temp_root,as_base=True,sid=sid)
            except BaseException:
                shutil.rmtree(temp_root,ignore_errors=True)
                raise
        else: return jsonify(error='Not found'),404
        with manager.acquire(sid) as store:
            return jsonify(ok=True,mode=store.data_mode,metadata=store.metadata())

    @app.get('/')
    @app.get('/<name>')
    def static(name='index.html'):
        mime={'index.html':'text/html; charset=utf-8','app.js':'text/javascript; charset=utf-8',
              'style.css':'text/css; charset=utf-8','world.geojson':'application/geo+json',
              'favicon.svg':'image/svg+xml','mosquito.gif':'image/gif','fly.png':'image/png'}
        if name not in mime: return jsonify(error='Not found'),404
        return Response(files('yoyoflot.product').joinpath('static',name).read_bytes(),content_type=mime[name])
    return app


def serve(product=None,host='127.0.0.1',port=8501,demo=False,scoring_path=None):
    from waitress import serve as serve_wsgi
    token=os.environ.get('YOYO_AUTH_TOKEN')
    if host not in {'127.0.0.1','localhost','::1'} and not token:
        raise ValueError('External binding requires YOYO_AUTH_TOKEN; local startup uses 127.0.0.1')
    path=scoring_path or os.environ.get('YOYO_SCORING_CONFIG')
    if path is None and default_scoring_path().is_file(): path=default_scoring_path()
    config=load_scoring(path) if path else None
    store=ProductStore(product,scoring=config)
    if store.runtime_scores is None:
        raise ValueError('Run build_and_run.sh to publish the final scoring product first')
    manager=StoreManager(store)
    print(f'YoYoFlot: {store.manifest["run_id"]}; HTTP port {port}; http://{host}:{port}/',flush=True)
    try:
        serve_wsgi(handler(manager,token,demo),host=host,port=port,threads=4,
                   connection_limit=64,channel_timeout=30,max_request_body_size=MAX_UPLOAD_BYTES+1024*1024)
    finally:
        manager.close()


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--product',default=os.environ.get('YOYO_PRODUCT'))
    p.add_argument('--scoring',default=os.environ.get('YOYO_SCORING_CONFIG'))
    p.add_argument('--demo',action='store_true',default=os.environ.get('YOYO_DEMO','').lower() in {'1','true','yes'})
    p.add_argument('--host',default=os.environ.get('YOYO_HOST','127.0.0.1'))
    p.add_argument('--port',type=int,default=int(os.environ.get('YOYO_PORT','8501')))
    a=p.parse_args(argv)
    if not a.product: p.error('--product is required')
    try:
        serve(a.product,a.host,a.port,a.demo,a.scoring)
    except (OSError,ValueError) as exc:
        p.error(str(exc))


if __name__=='__main__': main()
