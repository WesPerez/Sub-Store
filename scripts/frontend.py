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


def prepare_client_copy(root):
    helper = root / 'src/utils/clientShare.ts'
    if helper.exists():
        raise ValueError('Frontend client-share helper already exists')
    for component, quote in (('FileListItem', "'"), ('SubListItem', '"'), ('PreviewPanel', "'")):
        replace_once(root, f'src/components/{component}.vue',
                     f'import {{ copyText }} from {quote}@/utils/clipboard{quote};',
                     f'import {{ copyText }} from {quote}@/utils/clipboard{quote};\n'
                     "import { getClientShareUrl } from '@/utils/clientShare';")

    replace_once(root, 'src/components/FileListItem.vue',
                 '<button class="copy-sub-link" @click.stop="onClickCopyLink">',
                 '<button class="copy-sub-link" data-testid="copy-file-client-link" '
                 ':data-source-name="name" title="复制客户端订阅" aria-label="复制客户端订阅" '
                 ':disabled="props.disabled || copyingClientLink" @click.stop="onClickCopyLink">')
    replace_once(root, 'src/components/FileListItem.vue',
                 '''  const onClickCopyLink = async () => {
    const path = `/api/file/${encodeURIComponent(name)}`;
    const url = `${host.value}${path}`;

    try {
      await copyText(url);
      showNotify({ title: t('filePage.copyNotify.succeed', { path }) });
    } catch (error) {
      Toast.fail(t("filePage.copyNotify.failed", { e: error?.message ?? String(error) }));
    }
  };''',
                 '''  const copyingClientLink = ref(false);
  const onClickCopyLink = async () => {
    if (props.disabled || copyingClientLink.value) return;
    copyingClientLink.value = true;
    try {
      await copyText(await getClientShareUrl('file', name));
      showNotify({ title: '已复制客户端订阅链接，可在「分享」查看有效期或撤销' });
    } catch (error) {
      Toast.fail(t("filePage.copyNotify.failed", { e: error?.message ?? String(error) }));
    } finally {
      copyingClientLink.value = false;
    }
  };''')

    replace_once(root, 'src/components/SubListItem.vue',
                 '<button class="copy-sub-link" @click.stop="onClickCopyLink">',
                 '<button class="copy-sub-link" data-testid="copy-subscription-client-link" '
                 ':data-source-name="name" title="复制客户端订阅" aria-label="复制客户端订阅" '
                 ':disabled="props.disabled || copyingClientLink" @click.stop="onClickCopyLink">')
    replace_once(root, 'src/components/SubListItem.vue',
                 '''const onClickCopyLink = async () => {
  const url = `${host.value}/download/${
    props.type === "collection" ? "collection/" : ""
  }${encodeURIComponent(name)}`;

  try {
    await copyText(url);
    showNotify({ title: t(`subPage.copyNotify.${shareBtnVisible.value ? "succeedWithShare" : "succeed"}`) });
  } catch (error) {
    Toast.fail(t("subPage.copyNotify.failed", { e: error?.message ?? String(error) }));
  }
};''',
                 '''const copyingClientLink = ref(false);
const onClickCopyLink = async () => {
  if (props.disabled || copyingClientLink.value) return;
  copyingClientLink.value = true;
  try {
    await copyText(await getClientShareUrl(props.type === 'collection' ? 'col' : 'sub', name));
    showNotify({ title: '已复制客户端订阅链接，可在「分享」查看有效期或撤销' });
  } catch (error) {
    Toast.fail(t("subPage.copyNotify.failed", { e: error?.message ?? String(error) }));
  } finally {
    copyingClientLink.value = false;
  }
};''')

    replace_once(root, 'src/components/PreviewPanel.vue',
                 '<button class="copy-sub-link" @click.stop="targetCopy(platform.path)">',
                 '<button class="copy-sub-link" data-testid="copy-platform-client-link" '
                 ':data-target="platform.path || \'auto\'" :disabled="copyingClientLink" '
                 '@click.stop="targetCopy(platform.path)">')
    replace_once(root, 'src/components/PreviewPanel.vue',
                 '''  const targetCopy = async (path: string) => {
    const url = getUrl(path);
    try {
      await copyText(url);
      showNotify({ title: notify });
    } catch (error) {
      Toast.fail(t("globalNotify.copyFailed", { e: error?.message ?? String(error) }));
    }
  };''',
                 '''  const copyingClientLink = ref(false);
  const targetCopy = async (path: string) => {
    if (copyingClientLink.value) return;
    copyingClientLink.value = true;
    try {
      let clientUrl = getUrl(path);
      if (!url) {
        const shared = new URL(await getClientShareUrl(type === 'collection' ? 'col' : 'sub', name));
        new URL(clientUrl).searchParams.forEach((value, key) => shared.searchParams.set(key, value));
        clientUrl = shared.href;
      }
      await copyText(clientUrl);
      showNotify({ title: '已复制客户端订阅链接，可在「分享」查看有效期或撤销' });
    } catch (error) {
      Toast.fail(t("globalNotify.copyFailed", { e: error?.message ?? String(error) }));
    } finally {
      copyingClientLink.value = false;
    }
  };''')
    helper.write_text((ROOT / 'scripts/frontend-client-share.ts').read_text())


def prepare(root):
    config = settings()
    revision = subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'], text=True).strip()
    if revision != config['revision']:
        raise ValueError('Frontend checkout differs from the pinned commit')
    prepare_client_copy(root)
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
