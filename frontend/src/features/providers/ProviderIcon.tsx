/** 将稳定 provider id 映射为本地打包的品牌图标。 */

import deepSeekIconUrl from '@lobehub/icons-static-svg/icons/deepseek-color.svg';
import kimiIconUrl from '@lobehub/icons-static-svg/icons/kimi-color.svg';
import minimaxIconUrl from '@lobehub/icons-static-svg/icons/minimax-color.svg';

import glmIconUrl from '@/assets/provider-logos/glm.svg';
import mimoIconUrl from '@/assets/provider-logos/mimo.svg';

interface ProviderIconProps {
  providerId: string;
  displayName?: string;
  size?: number;
}

interface BrandIconDefinition {
  className: string;
  source: string;
}

const brandIcons: Record<string, BrandIconDefinition> = {
  deepseek: {
    className: 'provider-icon-avatar--deepseek',
    source: deepSeekIconUrl,
  },
  glm: {
    className: 'provider-icon-avatar--glm',
    source: glmIconUrl,
  },
  kimi: {
    className: 'provider-icon-avatar--kimi',
    source: kimiIconUrl,
  },
  minimax: {
    className: 'provider-icon-avatar--minimax',
    source: minimaxIconUrl,
  },
  mimo: {
    className: 'provider-icon-avatar--mimo',
    source: mimoIconUrl,
  },
};

const brandIconAliases: Record<string, string> = {
  moonshot: 'kimi',
  'xiaomi-mimo': 'mimo',
  xiaomi_mimo: 'mimo',
  zhipu: 'glm',
};

/** 为预置 provider 渲染专用图标，未知服务退回中性首字母。 */
export function ProviderIcon({
  providerId,
  displayName,
  size = 28,
}: ProviderIconProps) {
  const normalizedId = providerId.toLowerCase();
  const avatarStyle = { width: size, height: size };
  const brandIcon =
    brandIcons[brandIconAliases[normalizedId] ?? normalizedId] ?? null;

  if (brandIcon !== null) {
    return (
      <span
        className={`provider-icon-avatar ${brandIcon.className}`}
        style={avatarStyle}
        aria-hidden="true"
      >
        <img src={brandIcon.source} alt="" />
      </span>
    );
  }

  const fallback = (displayName ?? providerId).trim().slice(0, 1).toUpperCase();
  return (
    <span
      className="provider-icon-fallback"
      style={{ width: size, height: size }}
      aria-hidden="true"
    >
      {fallback || 'M'}
    </span>
  );
}
