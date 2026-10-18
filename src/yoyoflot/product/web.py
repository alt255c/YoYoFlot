"""Local read-only HTTP app. No data upload, execution endpoints or external assets."""
import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from urllib.parse import parse_qs, urlsplit

from .store import ProductStore


def handler(store):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path=urlsplit(self.path)
            try:
                if path.path=='/api/metadata':payload=store.metadata()
                elif path.path=='/api/top':payload=store.top()
                elif path.path=='/api/profile':
                    query=parse_qs(path.query,strict_parsing=True)
                    if set(query)!={'id'} or len(query['id'])!=1:raise ValueError('Expected one id parameter')
                    payload=store.profile(query['id'][0])
                elif path.path in {'/','/app.js','/style.css','/world.geojson','/favicon.svg'}:
                    name='index.html' if path.path=='/' else path.path[1:]
                    content=files('yoyoflot.product').joinpath('static',name).read_bytes()
                    mime={'index.html':'text/html; charset=utf-8','app.js':'text/javascript; charset=utf-8',
                          'style.css':'text/css; charset=utf-8','world.geojson':'application/geo+json','favicon.svg':'image/svg+xml'}[name]
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

        def send_json(self,status,payload):
            self.send_content(status,json.dumps(payload,ensure_ascii=False,allow_nan=False).encode(),'application/json; charset=utf-8')

        def send_content(self,status,content,mime):
            self.send_response(status)
            self.send_header('Content-Type',mime)
            self.send_header('Content-Length',str(len(content)))
            self.send_header('Cache-Control','no-store' if mime.startswith('application/json') else 'no-cache')
            self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers()
            self.wfile.write(content)
    return Handler


def serve(product, host='127.0.0.1', port=8501):
    store=ProductStore(product)
    with ThreadingHTTPServer((host,port),handler(store)) as server:
        print(f'YoYoFlot: {store.manifest["run_id"]}; HTTP port {server.server_port}',flush=True)
        try:server.serve_forever()
        except KeyboardInterrupt:pass


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--product',required=True)
    p.add_argument('--host',default='127.0.0.1')
    p.add_argument('--port',type=int,default=8501)
    a=p.parse_args(argv)
    serve(a.product,a.host,a.port)


if __name__=='__main__':main()
