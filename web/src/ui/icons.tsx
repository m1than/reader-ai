// App-owned inline SVG icons (WEB-UI FR-UI-11 review B1: the shell imports no @acme/ui/icon module, so the initial
// chunk stays within NFR-UI-1). 16px, stroke in currentColor, hidden from assistive technology; the control that holds
// an icon carries the accessible name.
import type { SVGProps } from 'react';

type IconProps = Omit<SVGProps<SVGSVGElement>, 'children'> & { size?: number };

const base = ({ size = 16, ...rest }: IconProps) => ({
  width: size,
  height: size,
  viewBox: '0 0 16 16',
  fill: 'none',
  stroke: 'currentColor',
  strokeWidth: 1.5,
  strokeLinecap: 'round' as const,
  strokeLinejoin: 'round' as const,
  'aria-hidden': true as const,
  focusable: 'false' as const,
  ...rest,
});

/** The reader-ai mark: an open book with a check (as on the sign-in page, SRV-AUTH FR-AUTH-19). */
export const MarkIcon = (p: IconProps) => (
  <svg {...base({ size: 20, ...p })} viewBox="0 0 20 20">
    <path d="M10 5.5C8.5 4.3 6.3 3.8 3 4v11c3.3-.2 5.5.3 7 1.5 1.5-1.2 3.7-1.7 7-1.5V4c-3.3-.2-5.5.3-7 1.5Z" />
    <path d="M10 5.5v11" />
    <path d="m12 10 1.5 1.5L16 8.5" />
  </svg>
);

/** A done lesson (WEB-MANAGE FR-MANAGE-4). */
export const CheckIcon = (p: IconProps) => (
  <svg {...base(p)}>
    <path d="m3.5 8.5 3 3 6-7" />
  </svg>
);

export const MenuIcon = (p: IconProps) => (
  <svg {...base(p)}>
    <path d="M2.5 4h11M2.5 8h11M2.5 12h11" />
  </svg>
);

export const ChevronIcon = (p: IconProps) => (
  <svg {...base(p)}>
    <path d="m6 3.5 4.5 4.5L6 12.5" />
  </svg>
);

export const PlayIcon = (p: IconProps) => (
  <svg {...base(p)} fill="currentColor" stroke="none">
    <path d="M5 3.2v9.6a.5.5 0 0 0 .76.43l7.7-4.8a.5.5 0 0 0 0-.86l-7.7-4.8A.5.5 0 0 0 5 3.2Z" />
  </svg>
);

export const MoreIcon = (p: IconProps) => (
  <svg {...base(p)} fill="currentColor" stroke="none">
    <circle cx="3.5" cy="8" r="1.25" />
    <circle cx="8" cy="8" r="1.25" />
    <circle cx="12.5" cy="8" r="1.25" />
  </svg>
);

export const CopyIcon = (p: IconProps) => (
  <svg {...base(p)}>
    <rect x="5.5" y="5.5" width="8" height="8" rx="1.5" />
    <path d="M10.5 5.5v-2a1 1 0 0 0-1-1h-6a1 1 0 0 0-1 1v6a1 1 0 0 0 1 1h2" />
  </svg>
);

export const CloseIcon = (p: IconProps) => (
  <svg {...base(p)}>
    <path d="m4 4 8 8M12 4l-8 8" />
  </svg>
);

export const InfoIcon = (p: IconProps) => (
  <svg {...base(p)}>
    <circle cx="8" cy="8" r="6" />
    <path d="M8 7.25V11M8 5h.01" />
  </svg>
);

export const WarningIcon = (p: IconProps) => (
  <svg {...base(p)}>
    <path d="M7.13 2.5 1.6 12a1 1 0 0 0 .87 1.5h11.06a1 1 0 0 0 .87-1.5L8.87 2.5a1 1 0 0 0-1.74 0Z" />
    <path d="M8 6.25V9M8 11.25h.01" />
  </svg>
);

export const AskIcon = (p: IconProps) => (
  <svg {...base(p)}>
    <path d="M2.5 3.5h11v7h-6l-3 2.5v-2.5h-2Z" />
  </svg>
);

export const SearchOffIcon = (p: IconProps) => (
  <svg {...base(p)}>
    <circle cx="7" cy="7" r="4.5" />
    <path d="m10.5 10.5 3 3M5.5 5.5l3 3M8.5 5.5l-3 3" />
  </svg>
);
