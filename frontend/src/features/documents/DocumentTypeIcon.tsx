/** 为翻译 composer 与任务列表提供统一、易辨认的彩色文件类型图标。 */

export type DocumentTypeKind = 'docx' | 'pptx' | 'txt' | 'md' | 'pdf';

interface DocumentTypeIconProps {
  kind: DocumentTypeKind;
  size?: number;
}

/** 使用紧凑的 code-native SVG 表达文件格式，保证小尺寸下仍清晰可辨。 */
export function DocumentTypeIcon({ kind, size = 28 }: DocumentTypeIconProps) {
  if (kind === 'docx') {
    return (
      <svg
        aria-hidden="true"
        className="document-type-icon"
        width={size}
        height={size}
        viewBox="0 0 32 32"
        fill="none"
      >
        <rect x="7" y="3" width="22" height="26" rx="4" fill="#2B7CD3" />
        <rect x="12" y="6" width="13" height="20" rx="2" fill="#FFFFFF" />
        <path
          d="M15 11H22M15 15H22M15 19H21M15 23H20"
          stroke="#76A9E3"
          strokeWidth="1.6"
          strokeLinecap="round"
        />
        <rect x="2" y="8" width="13" height="16" rx="3" fill="#185ABD" />
        <path
          d="M4.7 12.2L6.2 19.7L8.5 14.4L10.7 19.7L12.3 12.2"
          stroke="#FFFFFF"
          strokeWidth="1.7"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      </svg>
    );
  }

  if (kind === 'pptx') {
    return (
      <svg
        aria-hidden="true"
        className="document-type-icon"
        width={size}
        height={size}
        viewBox="0 0 32 32"
        fill="none"
      >
        <rect x="7" y="3" width="22" height="26" rx="4" fill="#E76F3D" />
        <rect x="12" y="7" width="13" height="18" rx="2" fill="#FFFFFF" />
        <path
          d="M15 20H22"
          stroke="#F0A27F"
          strokeWidth="1.7"
          strokeLinecap="round"
        />
        <circle cx="18.5" cy="14" r="4" fill="#F6B195" />
        <path d="M18.5 10V14H22.5" fill="#D94F22" />
        <rect x="2" y="8" width="13" height="16" rx="3" fill="#C43E1C" />
        <path
          d="M6 19V12.5H8.5C10.1 12.5 11 13.35 11 14.7C11 16.1 10.05 16.95 8.45 16.95H7.55V19H6ZM7.55 15.65H8.35C9.1 15.65 9.5 15.33 9.5 14.72C9.5 14.12 9.1 13.8 8.35 13.8H7.55V15.65Z"
          fill="#FFFFFF"
        />
      </svg>
    );
  }

  if (kind === 'md') {
    return (
      <svg
        aria-hidden="true"
        className="document-type-icon"
        width={size}
        height={size}
        viewBox="0 0 32 32"
        fill="none"
      >
        <rect x="3" y="5" width="26" height="22" rx="4" fill="#343A40" />
        <path
          d="M7 20V12L10.5 15.8L14 12V20"
          stroke="#FFFFFF"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
        <path
          d="M21 11.5V19M17.8 16.2L21 19.4L24.2 16.2"
          stroke="#8FD0DD"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      </svg>
    );
  }

  if (kind === 'pdf') {
    return (
      <svg
        aria-hidden="true"
        className="document-type-icon"
        width={size}
        height={size}
        viewBox="0 0 32 32"
        fill="none"
      >
        <path
          d="M7 3H20L27 10V27C27 28.1 26.1 29 25 29H7C5.9 29 5 28.1 5 27V5C5 3.9 5.9 3 7 3Z"
          fill="#E5484D"
        />
        <path d="M20 3V10H27" fill="#F58B8E" />
        <path
          d="M9 21C12.5 17.8 14.3 13.3 15.1 9.5C16.3 14.6 18.3 18.1 23.5 20.5C18 19.5 13.9 19.7 9 21Z"
          stroke="#FFFFFF"
          strokeWidth="1.55"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      </svg>
    );
  }

  return (
    <svg
      aria-hidden="true"
      className="document-type-icon"
      width={size}
      height={size}
      viewBox="0 0 32 32"
      fill="none"
    >
      <path
        d="M7 3H20L27 10V27C27 28.1 26.1 29 25 29H7C5.9 29 5 28.1 5 27V5C5 3.9 5.9 3 7 3Z"
        fill="#4F8292"
      />
      <path d="M20 3V10H27" fill="#8DB0BA" />
      <path
        d="M10 15H22M10 19H22M10 23H18"
        stroke="#FFFFFF"
        strokeWidth="1.8"
        strokeLinecap="round"
      />
    </svg>
  );
}
