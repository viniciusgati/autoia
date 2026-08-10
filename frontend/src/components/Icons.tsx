/** Ícones SVG inline para a UI. Cada componente aceita `size` (default 18). */

import type { SVGProps } from "react";

interface IconProps extends SVGProps<SVGSVGElement> {
  size?: number;
}

function icon(path: React.ReactNode, defaultSize = 18) {
  return ({ size = defaultSize, ...props }: IconProps) => (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.8}
      strokeLinecap="round"
      strokeLinejoin="round"
      {...props}
    >
      {path}
    </svg>
  );
}

export const DashboardIcon = icon(
  <>
    <rect x="3" y="3" width="7" height="7" rx="1" />
    <rect x="14" y="3" width="7" height="7" rx="1" />
    <rect x="3" y="14" width="7" height="7" rx="1" />
    <rect x="14" y="14" width="7" height="7" rx="1" />
  </>,
);

export const TasksIcon = icon(
  <>
    <path d="M9 5H7a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V7a2 2 0 0 0-2-2h-2" />
    <rect x="9" y="3" width="6" height="4" rx="1" />
    <path d="M9 14l2 2 4-4" />
  </>,
);

export const RobotsIcon = icon(
  <>
    <rect x="3" y="6" width="18" height="12" rx="3" />
    <circle cx="9" cy="12" r="1.5" fill="currentColor" />
    <circle cx="15" cy="12" r="1.5" fill="currentColor" />
    <path d="M9 17h6" />
    <path d="M12 2v4" />
  </>,
);

export const PipelinesIcon = icon(
  <>
    <path d="M4 20h16" />
    <path d="M4 20V4" />
    <path d="M8 12h2" />
    <path d="M14 8h2" />
    <path d="M18 16h2" />
  </>,
);

export const ProjectsIcon = icon(
  <>
    <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2v11z" />
  </>,
);

export const SettingsIcon = icon(
  <>
    <circle cx="12" cy="12" r="3" />
    <path d="M12 1v2M12 21v2M4.22 4.22l1.42 1.42M18.36 18.36l1.42 1.42M1 12h2M21 12h2M4.22 19.78l1.42-1.42M18.36 5.64l1.42-1.42" />
  </>,
);

export const PlusIcon = icon(
  <>
    <path d="M12 5v14M5 12h14" />
  </>,
);

export const PlayIcon = icon(
  <polygon points="6 4 20 12 6 20" fill="currentColor" stroke="none" />,
);

export const ChevronRightIcon = icon(
  <polyline points="9 18 15 12 9 6" />,
);

export const ExternalLinkIcon = icon(
  <>
    <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6" />
    <polyline points="15 3 21 3 21 9" />
    <line x1="10" y1="14" x2="21" y2="3" />
  </>,
);

export const AlertIcon = icon(
  <>
    <path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z" />
    <line x1="12" y1="9" x2="12" y2="13" />
    <line x1="12" y1="17" x2="12.01" y2="17" />
  </>,
);

export const CheckIcon = icon(
  <polyline points="20 6 9 17 4 12" />,
);

export const XIcon = icon(
  <>
    <line x1="18" y1="6" x2="6" y2="18" />
    <line x1="6" y1="6" x2="18" y2="18" />
  </>,
);

export const TerminalIcon = icon(
  <polyline points="4 17 10 11 4 5" />,
);

export const GitBranchIcon = icon(
  <>
    <line x1="6" y1="3" x2="6" y2="15" />
    <circle cx="6" cy="3" r="2" />
    <circle cx="6" cy="18" r="2" />
    <path d="M18 9a2 2 0 0 0-2 2v4a2 2 0 0 1-2 2H6" />
    <circle cx="18" cy="9" r="2" />
  </>,
);

export const RefreshIcon = icon(
  <>
    <polyline points="23 4 23 10 17 10" />
    <polyline points="1 20 1 14 7 14" />
    <path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15" />
  </>,
);

export const ArrowLeftIcon = icon(
  <polyline points="15 18 9 12 15 6" />,
);
