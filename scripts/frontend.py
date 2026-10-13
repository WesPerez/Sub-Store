#!/usr/bin/env python3
"""Pin and adapt the official frontend for this deployment's URL prefix."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def settings():
    value = json.loads((ROOT / 'deploy/frontend.json').read_text())
    if (value['repository'] != 'sub-store-org/Sub-Store-Front-End'
            or not re.fullmatch('[0-9a-f]{40}', value['revision'])
            or value['public_path'] != '/substore/'
            or value['api_path'] != '/substore/backend'):
        raise ValueError('Unexpected frontend deployment identity')
    return value


def replace_once(root, name, before, after):
    path = root / name
    source = path.read_text()
    if source.count(before) != 1:
        raise ValueError(f'Frontend patch context changed: {name}')
    path.write_text(source.replace(before, after, 1))


def prepare(root):
    config = settings()
    revision = subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'], text=True).strip()
    if revision != config['revision']:
        raise ValueError('Frontend checkout differs from the pinned commit')
    replace_once(root, 'src/router/index.ts', 'createWebHistory()',
                 'createWebHistory(import.meta.env.BASE_URL)')
    replace_once(root, 'src/store/subs.ts',
                 "window.location.pathname === '/subs'",
                 "window.location.pathname.replace(/\\/$/, '') === import.meta.env.BASE_URL.replace(/\\/$/, '') + '/subs'")
    replace_once(root, 'src/views/settings/APISetting.vue',
                 'new URL(window.location.origin)',
                 'new URL(import.meta.env.BASE_URL, window.location.origin)')
    for key in ('id', 'start_url', 'scope'):
        replace_once(root, 'vite.config.ts', f'{key}: "/",',
                     f'{key}: env.VITE_PUBLIC_PATH,')
    replace_once(root, 'vite.config.ts', 'navigateFallback: "/index.html",',
                 'navigateFallback: env.VITE_PUBLIC_PATH + "index.html",')
    replace_once(root, 'vite.config.ts', 'cacheName: "js-cache",',
                 'cacheName: "sub-store-js-cache",')
    replace_once(root, 'vite.config.ts', 'cacheName: "asset-cache",',
                 'cacheName: "sub-store-asset-cache",')
    # VitePWA emits the canonical manifest link. Avoid two competing manifests.
    replace_once(root, 'index.html', '  <link rel="manifest" href="/manifests.json" />\n', '')
    manifest_path = root / 'public/manifests.json'
    manifest = json.loads(manifest_path.read_text())
    for key in ('id', 'start_url', 'scope'):
        manifest[key] = config['public_path']
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
    path = root / '.env.production'
    source = path.read_text()
    source = re.sub(r'^VITE_(?:PUBLIC_PATH|API_URL)\s*=.*\n?', '', source, flags=re.M)
    path.write_text(source.rstrip() + '\nVITE_PUBLIC_PATH=/substore/\nVITE_API_URL=/substore/backend\n')


def verify(root):
    dist = root / 'dist'
    html = (dist / 'index.html').read_text()
    links = re.findall(r'<link\b[^>]*rel=["\']manifest["\'][^>]*>', html)
    if len(links) != 1:
        raise ValueError('Exactly one PWA manifest must be linked')
    for url in re.findall(r'(?:src|href)=["\'](/[^"\']*)', html):
        if not url.startswith('/substore/'):
            raise ValueError('Frontend asset escaped the application prefix')
    manifest_name = re.search(r'href=["\']([^"\']+)', links[0])[1].removeprefix('/substore/')
    manifest = json.loads((dist / manifest_name).read_text())
    if any(manifest[key] != '/substore/' for key in ('id', 'start_url', 'scope')):
        raise ValueError('PWA must stay inside /substore/')
    if '/substore/index.html' not in (dist / 'sw.js').read_text():
        raise ValueError('Service worker navigation escaped the application prefix')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('metadata', 'prepare', 'verify'))
    parser.add_argument('directory', type=Path, nargs='?', default=ROOT / 'frontend-src')
    args = parser.parse_args()
    if args.command == 'metadata':
        output = '\n'.join(f'{key}={value}' for key, value in settings().items()) + '\n'
        if os.environ.get('GITHUB_OUTPUT'):
            with open(os.environ['GITHUB_OUTPUT'], 'a') as stream:
                stream.write(output)
        else:
            print(output, end='')
    elif args.command == 'prepare':
        prepare(args.directory)
    else:
        verify(args.directory)


if __name__ == '__main__':
    main()
