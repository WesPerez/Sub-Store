#!/usr/bin/env python3
"""Exercise the production image behind real Nginx using synthetic subscriptions."""
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import tempfile
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
BASE = 'http://127.0.0.1:18080/substore'
API = BASE + '/backend/api'
OUTPUT = ROOT / 'acceptance-output'
GIB = 1024**3
EXPIRES = 2524608000
FLOW_HEADER = f'upload={GIB}; download={2 * GIB}; total={10 * GIB}; expire={EXPIRES}'
NODES = {
    'a': 'proxies:\n  - {name: CI-Node-A, type: ss, server: node-a.example.invalid, port: 443, cipher: aes-128-gcm, password: synthetic-a}\n',
    'b': 'proxies:\n  - {name: CI-Node-B, type: ss, server: node-b.example.invalid, port: 443, cipher: aes-128-gcm, password: synthetic-b}\n',
}


class Upstream(BaseHTTPRequestHandler):
    requests = []

    def do_GET(self):
        self.respond(True)

    def do_HEAD(self):
        self.respond(False)

    def respond(self, body):
        self.requests.append(self.command)
        value = NODES['a'].encode()
        self.send_response(200)
        self.send_header('Content-Type', 'text/yaml')
        self.send_header('Content-Length', str(len(value)))
        self.send_header('subscription-userinfo', FLOW_HEADER)
        self.end_headers()
        if body:
            self.wfile.write(value)

    def log_message(self, *_):
        pass


def command(*args, **kwargs):
    return subprocess.check_output([str(arg) for arg in args], text=True, **kwargs).strip()


def request(url, *, method='GET', data=None, password=None):
    headers = {}
    if password:
        headers['Authorization'] = 'Basic ' + base64.b64encode(('ci:' + password).encode()).decode()
    if data is not None:
        headers['Content-Type'] = 'application/json'
        data = json.dumps(data).encode()
    try:
        response = urlopen(Request(url, data=data, headers=headers, method=method), timeout=20)
    except HTTPError as error:
        response = error
    with response:
        return response.status, response.read().decode(), dict(response.headers)


def wait_ready(url, expected=200):
    for _ in range(90):
        try:
            if request(url)[0] == expected:
                return
        except (URLError, OSError):
            pass
        time.sleep(1)
    raise AssertionError('Isolated service did not become ready')


def api(path, password, *, data=None, method=None):
    code, body, _ = request(API + path, password=password, data=data,
                            method=method or ('POST' if data is not None else 'GET'))
    value = json.loads(body)
    assert code in (200, 201) and value['status'] == 'success', (path, code, value)
    return value.get('data')


def verify_http(password, gateway, port):
    for path in ('/', '/backend/api/subs', '/backend/api/utils/env'):
        assert request(BASE + path)[0] == 401
    for path in ('/api/subs', '/download/source-a'):
        assert request(BASE + path)[0] == 404
    env = api('/utils/env', password)
    assert env['meta']['node']['env']['SUB_STORE_FRONTEND_BACKEND_PATH'] == '/backend'
    for key in ('a', 'b'):
        payload = {'name': 'source-' + key, 'displayName': 'CI Source ' + key.upper(),
                   'source': 'remote' if key == 'a' else 'local', 'process': []}
        if key == 'a':
            payload['url'] = f'http://{gateway}:{port}/synthetic.yaml'
        else:
            payload.update(content=NODES[key], subUserinfo=FLOW_HEADER)
        api('/subs', password, data=payload)
        flow = api('/sub/flow/source-' + key, password)
        assert flow['usage'] == {'upload': GIB, 'download': 2 * GIB}, flow
        assert flow['total'] == 10 * GIB and flow['expires'] == EXPIRES, flow
    api('/collections', password, data={'name': 'all-nodes', 'displayName': 'All CI Nodes',
                                       'subscriptions': ['source-a', 'source-b'], 'process': []})
    token = api('/token', password, data={'payload': {'type': 'col', 'name': 'all-nodes'},
                                        'options': {'expiresIn': '1d'}})['token']
    share = BASE + '/share/col/all-nodes?' + urlencode({'token': token, 'target': 'ClashMeta'})
    code, body, _ = request(share)
    assert code == 200 and all(node in body for node in ('CI-Node-A', 'CI-Node-B')), (code, body)
    assert all(host in body for host in ('node-a.example.invalid', 'node-b.example.invalid'))
    assert request(BASE + '/share/col/all-nodes')[0] == 404
    assert request(share.replace(token, 'invalid-token'))[0] == 404
    assert request(share.replace('/col/all-nodes', '/sub/source-a'))[0] == 404
    assert request(share, method='POST')[0] == 405

    template = 'mixed-port: 7890\nproxy-groups:\n  - name: Proxy\n    type: select\n    include-all: true\nrules:\n  - MATCH,Proxy\n'
    api('/files', password, data={'name': 'mihomo', 'type': 'mihomoConfig',
                                 'sourceType': 'collection', 'sourceName': 'all-nodes',
                                 'process': [{'type': 'Script Operator', 'args': {'mode': 'script', 'content': template}}]})
    file_token = api('/token', password, data={'payload': {'type': 'file', 'name': 'mihomo'},
                                              'options': {'expiresIn': '1d'}})['token']
    code, body, _ = request(BASE + '/share/file/mihomo?' + urlencode({'token': file_token}))
    assert code == 200 and all(value in body for value in ('CI-Node-A', 'CI-Node-B', 'mixed-port: 7890', 'proxy-groups:', 'MATCH,Proxy')), (code, body)
    api('/token/' + token + '?type=col&name=all-nodes', password, method='DELETE')
    assert request(share)[0] == 404
    assert 'GET' in Upstream.requests and 'HEAD' in Upstream.requests, Upstream.requests


def verify_browser(password):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(http_credentials={'username': 'ci', 'password': password},
                                      viewport={'width': 1440, 'height': 1000}, locale='zh-CN')
        page = context.new_page()
        errors, responses, flow_names = [], [], set()
        page.on('pageerror', lambda error: errors.append(str(error)))

        def response_seen(response):
            parsed = urlsplit(response.url)
            if parsed.netloc == '127.0.0.1:18080':
                responses.append((parsed.path, response.status))
                if '/api/sub/flow/' in parsed.path and response.status == 200:
                    flow_names.add(parsed.path.rsplit('/', 1)[-1])

        page.on('response', response_seen)
        for suffix in ('/subs', '/subs/'):
            flow_names.clear()
            page.goto(BASE + suffix, wait_until='networkidle')
            try:
                page.wait_for_function("document.body.innerText.includes('3 GB / 10 GB')", timeout=30000)
            except Exception:
                page.screenshot(path=str(OUTPUT / 'failure.png'), full_page=True)
                details = {'url': page.url, 'body': page.locator('body').inner_text(),
                           'page_errors': errors, 'responses': responses, 'flow_sources': sorted(flow_names)}
                (OUTPUT / 'failure.json').write_text(json.dumps(details, indent=2) + '\n')
                print(json.dumps(details, ensure_ascii=False), flush=True)
                raise
            assert {'source-a', 'source-b'} <= flow_names, flow_names
            assert '/substore/subs' in page.url
        page.wait_for_function("navigator.serviceWorker.getRegistration().then(r => !!r)", timeout=30000)
        scope = page.evaluate('navigator.serviceWorker.getRegistration().then(r => r.scope)')
        assert scope == 'http://127.0.0.1:18080/substore/', scope
        assert not errors, errors
        assert not [(path, code) for path, code in responses if code >= 400], responses
        assert all(path.startswith('/substore/') for path, _ in responses), responses
        page.screenshot(path=str(OUTPUT / 'subscriptions.png'), full_page=True)
        (OUTPUT / 'browser.json').write_text(json.dumps({'scope': scope, 'flow_sources': sorted(flow_names),
                                                        'page_errors': errors, 'responses': responses}, indent=2) + '\n')
        browser.close()


def main():
    if os.environ.get('GITHUB_ACTIONS') != 'true' or os.environ.get('RUNNER_ENVIRONMENT') != 'github-hosted':
        raise SystemExit('Synthetic deployment acceptance runs only on a GitHub-hosted runner')
    image = os.environ['SUBSTORE_TEST_IMAGE']
    assert re.fullmatch(r'ghcr.io/wesperez/sub-store:sha-[a-f0-9]{40}', image)
    OUTPUT.mkdir(exist_ok=True)
    upstream = ThreadingHTTPServer(('0.0.0.0', 0), Upstream)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    with tempfile.TemporaryDirectory(prefix='substore-acceptance-', dir=os.environ['RUNNER_TEMP']) as temporary:
        root = Path(temporary)
        data = root / 'data'
        data.mkdir(mode=0o755)
        subprocess.run(['sudo', 'chown', '1000:1000', str(data)], check=True)
        compose_file = root / 'docker-compose.yml'
        compose_file.write_text((ROOT / 'deploy/docker-compose.yml').read_text()
                                .replace('/var/lib/sub-store/data', str(data))
                                .replace('container_name: sub-store', 'container_name: sub-store-ci'))
        compose = ['docker', 'compose', '--project-name', 'sub-store-ci', '-f', str(compose_file)]
        environment = {**os.environ, 'SUB_STORE_IMAGE': image}
        password = secrets.token_urlsafe(18)
        digest = command('openssl', 'passwd', '-apr1', '-stdin', input=password + '\n')
        (root / 'htpasswd').write_text('ci:' + digest + '\n')
        snippet = (ROOT / 'deploy/nginx-substore.conf').read_text().replace('/var/log/sub-store/nginx-error.log', '/dev/stderr')
        (root / 'nginx.conf').write_text('events {}\nhttp { server { listen 18080; server_name localhost;\n' + snippet + '\nlocation / { return 404; }\n} }\n')
        try:
            command(*compose, 'up', '-d', '--no-build', '--pull', 'never', env=environment)
            wait_ready('http://127.0.0.1:13001/backend/api/utils/env')
            inspected = json.loads(command('docker', 'inspect', 'sub-store-ci'))[0]
            gateway = next(iter(inspected['NetworkSettings']['Networks'].values()))['Gateway']
            command('docker', 'run', '-d', '--name', 'sub-store-ci-nginx', '--network', 'host',
                    '-v', str(root / 'nginx.conf') + ':/etc/nginx/nginx.conf:ro',
                    '-v', str(root / 'htpasswd') + ':/etc/sub-store/htpasswd:ro', 'nginx:1.28-alpine')
            wait_ready(BASE + '/', 401)
            verify_http(password, gateway, upstream.server_address[1])
            verify_browser(password)
            state = json.loads(command('docker', 'inspect', '--format', '{{json .State}}', 'sub-store-ci'))
            assert state['Health']['Status'] == 'healthy', state['Health']['Status']
            (OUTPUT / 'acceptance.json').write_text(json.dumps({'status': 'passed', 'revision': os.environ['GITHUB_SHA'],
                'checks': ['management-auth', 'subpath-ui', 'remote-flow-headers', 'local-flow', 'collection-all-nodes',
                           'unchanged-upstream-hosts', 'share-scoping', 'invalid-token', 'revoked-token',
                           'mihomo-template', 'pwa-scope', 'container-health']}, indent=2) + '\n')
            print('Sub-Store HTTP, browser, subscription metadata and share acceptance passed')
        finally:
            for container in ('sub-store-ci', 'sub-store-ci-nginx'):
                logs = subprocess.run(['docker', 'logs', container], text=True, capture_output=True)
                (OUTPUT / (container + '.log')).write_text(logs.stdout + logs.stderr)
            subprocess.run(['docker', 'rm', '-f', 'sub-store-ci-nginx'], capture_output=True)
            subprocess.run([*compose, 'down', '--volumes'], env=environment, capture_output=True)
            upstream.shutdown()
            subprocess.run(['sudo', 'chown', '-R', str(os.getuid()) + ':' + str(os.getgid()), str(data)], check=True)


if __name__ == '__main__':
    main()
