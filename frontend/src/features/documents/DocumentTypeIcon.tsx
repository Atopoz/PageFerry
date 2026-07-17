/** 为翻译 composer 与任务列表提供统一、易辨认的彩色文件类型图标。 */

import { useId } from 'react';

export type DocumentTypeKind = 'docx' | 'pptx' | 'xlsx' | 'txt' | 'md' | 'pdf';

interface DocumentTypeIconProps {
  kind: DocumentTypeKind;
  size?: number;
}

type DocumentGlyph = 'document' | 'presentation' | 'acrobat';

interface DocumentTypeSkin {
  page: string;
  fold: string;
  label: string;
  glyph: DocumentGlyph;
  glyphColor: string;
  uppercase?: boolean;
}

/**
 * 视觉参数与 react-file-icon（MIT 许可证）的 defaultStyles 对齐，
 * 保持和 JOTO-Translation 历史任务列表一致的图标观感：
 * 彩色纸面、右上角深色折角、半透明内容 glyph、底部色带标注扩展名。
 */
const skins: Record<DocumentTypeKind, DocumentTypeSkin> = {
  docx: {
    page: '#2C5898',
    fold: '#254A80',
    label: '#2C5898',
    glyph: 'document',
    glyphColor: 'rgba(255,255,255,0.4)',
    uppercase: true,
  },
  pptx: {
    page: '#D14423',
    fold: '#AB381D',
    label: '#D14423',
    glyph: 'presentation',
    glyphColor: 'rgba(255,255,255,0.4)',
    uppercase: true,
  },
  xlsx: {
    page: '#217346',
    fold: '#1B5E3A',
    label: '#217346',
    glyph: 'document',
    glyphColor: 'rgba(255,255,255,0.4)',
    uppercase: true,
  },
  txt: {
    page: '#F5F5F5',
    fold: '#DBDBDB',
    label: '#A8A8A8',
    glyph: 'document',
    glyphColor: '#CECECE',
  },
  md: {
    page: '#F5F5F5',
    fold: '#DBDBDB',
    label: '#A8A8A8',
    glyph: 'document',
    glyphColor: '#CECECE',
  },
  pdf: {
    page: '#F5F5F5',
    fold: '#DBDBDB',
    label: '#D93831',
    glyph: 'acrobat',
    glyphColor: '#CECECE',
  },
};

/** glyph 路径来自 react-file-icon（MIT），viewBox 同为 40×48。 */
const glyphs: Record<DocumentGlyph, { d: string; transform: string }> = {
  document: {
    d: 'M12 4H0v2h12V4zM0 10h18V8H0v2zM0 0v2h18V0H0z',
    transform: 'translate(15 15)',
  },
  presentation: {
    d: 'M2 4H0v10c0 1.1.9 2 2 2h14v-2H2V4zm16-4H6C4.9 0 4 .9 4 2v8c0 1.1.9 2 2 2h12c1.1 0 2-.9 2-2V2c0-1.1-.9-2-2-2zm0 10H6V2h12v8z',
    transform: 'matrix(-1 0 0 1 34 12)',
  },
  acrobat: {
    d: 'M10.15 1.095C9.938.33 9.42-.051 8.984.005c-.528.068-1.09.382-1.314.876-.63 1.416.685 5.582.887 6.279-1.28 3.863-5.66 11.5-7.806 12.017-.045-.505.225-1.965 3.055-3.785.146-.157.315-.348.393-.472-2.392 1.168-5.492 3.044-3.628 4.448.102.079.259.146.439.213 1.426.528 3.425-1.201 5.435-5.121 2.213-.73 3.999-1.28 6.526-1.662 2.762 1.875 4.616 2.257 5.874 1.774.348-.135.898-.573 1.055-1.145-1.022 1.258-3.414.382-5.323-.82 1.763-.191 3.582-.303 4.369-.056 1 .314.965.808.954.876.079-.27.191-.708-.022-1.056-.842-1.37-4.706-.573-6.11-.427-2.212-1.336-3.74-3.717-4.358-5.436.573-2.212 1.19-3.818.742-5.413zm-.954 4.638C8.826 4.42 8.309 1.5 9.14.556c1.628.932.618 3.144.056 5.177zm3.044 6.514c-2.134.393-3.583.944-5.66 1.764.617-1.202 1.785-4.268 2.346-6.29.787 1.573 1.741 3.111 3.314 4.526z',
    transform: 'translate(14 9)',
  },
};

/** 40×48 的经典文件图标：彩色纸面 + 折角 + 内容 glyph + 扩展名色带。 */
export function DocumentTypeIcon({ kind, size = 30 }: DocumentTypeIconProps) {
  const skin = skins[kind];
  const glyph = glyphs[skin.glyph];
  const gradientId = useId();
  const label = skin.uppercase ? kind.toUpperCase() : kind;

  return (
    <svg
      aria-hidden="true"
      className="document-type-icon"
      width={size}
      height={(size * 48) / 40}
      viewBox="0 0 40 48"
      fill="none"
    >
      <defs>
        <linearGradient id={gradientId} x1="100%" y1="0%" x2="0%" y2="100%">
          <stop offset="0%" stopColor="#FFFFFF" stopOpacity="0.25" />
          <stop offset="66.67%" stopColor="#FFFFFF" stopOpacity="0" />
        </linearGradient>
      </defs>

      <path
        d="M4 0H28L40 12V44A4 4 0 0 1 36 48H4A4 4 0 0 1 0 44V4A4 4 0 0 1 4 0Z"
        fill={skin.page}
      />
      <path
        d="M4 0H28L40 12V44A4 4 0 0 1 36 48H4A4 4 0 0 1 0 44V4A4 4 0 0 1 4 0Z"
        fill={`url(#${gradientId})`}
      />
      <path d="M28 0L40 12H28V0Z" fill={skin.fold} />
      <g fill={skin.glyphColor} fillRule="evenodd">
        <path d={glyph.d} transform={glyph.transform} />
      </g>

      <path
        d="M0 34H40V44A4 4 0 0 1 36 48H4A4 4 0 0 1 0 44V34Z"
        fill={skin.label}
      />
      <text
        x="20"
        y="44"
        fill="#FFFFFF"
        fontFamily="-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif"
        fontSize="9"
        fontWeight="bold"
        textAnchor="middle"
        style={{ userSelect: 'none', pointerEvents: 'none' }}
      >
        {label}
      </text>
    </svg>
  );
}
