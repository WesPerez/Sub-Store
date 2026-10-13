/** Client exports must use scoped shares; management downloads require login. */
import { useShareApi } from '@/api/share';
import { getHostAPIUrl, getHostAPIShareBaseUrl } from '@/hooks/useHostAPI';
import { useGlobalStore } from '@/store/global';
import { useSubsStore } from '@/store/subs';
import { getSharePublicUrl } from '@/utils/share';

type SourceType = 'sub' | 'col' | 'file';
const pending = new Map<string, Promise<string>>();

const reusable = (share: Share, type: SourceType, name: string, now: number) => {
  if (share.type !== type || share.name !== name || !share.token
    || share['age-public-key'] || share.mode === 'count') return false;
  // Automatic subscription updates need an unencrypted, unmetered link.
  const expiry = share.exp ?? share.expiresAt;
  if (expiry != null) return Number.isFinite(Number(expiry)) && Number(expiry) > now;
  return !share.mode && !share.expiresIn;
};

export async function getClientShareUrl(type: SourceType, name: string): Promise<string> {
  const host = new URL(getHostAPIUrl(), window.location.origin).href.replace(/\/$/, '');
  const shareBaseUrl = getHostAPIShareBaseUrl();
  const environment = useGlobalStore().env;
  if (!environment?.backend) throw new Error('正在加载服务器信息，请稍后重试');
  if (!environment?.feature?.share) throw new Error('服务器尚未启用分享功能');
  const secretPath = environment?.meta?.node?.env?.SUB_STORE_FRONTEND_BACKEND_PATH || '';
  const identity = { host, shareBaseUrl, secretPath, type, name };
  // Validate the public URL before creating a share. Never fall back to an API URL.
  getSharePublicUrl({ ...identity, token: 'validate' });
  const key = JSON.stringify(identity);
  let operation = pending.get(key);
  if (!operation) {
    operation = (async () => {
      const api = useShareApi();
      // Read the server every time so revoked tokens cannot survive in a UI cache.
      const response = await api.getShares(type, name);
      if (response.data.status !== 'success' || !Array.isArray(response.data.data)) {
        throw new Error('无法读取分享链接，请重试');
      }
      const shares = response.data.data as Share[];
      let token = shares
        .find(share => reusable(share, type, name, Date.now()))?.token;
      if (!token) {
        if (shares.some(share => share['age-public-key'] || share.mode === 'count')) {
          throw new Error('该对象设有加密或次数限制，请到「分享」复制指定链接');
        }
        const created = await api.createShare({
          payload: { type, name, displayName: name, remark: '客户端订阅' },
          options: { mode: 'duration', expiresIn: '3650d' },
        });
        if (created.data.status !== 'success') throw new Error('无法生成分享链接，请重试');
        token = created.data.data?.token;
        if (typeof token !== 'string' || !token) {
          throw new Error('无法生成分享链接，请重试');
        }
        await useSubsStore().fetchShareData();
      }
      return getSharePublicUrl({ ...identity, token });
    })();
    pending.set(key, operation);
  }
  try {
    return await operation;
  } finally {
    if (pending.get(key) === operation) pending.delete(key);
  }
}
