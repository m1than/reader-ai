// One block for every empty, not-found, not-published and error state (WEB-UI FR-UI-11, design pass): an icon, the
// state's h1 in the app page-title style, a muted explanation and the actions in one row.
import type { ReactNode } from 'react';

export function StateBlock({
  icon,
  title,
  children,
  actions,
  tone = 'neutral',
}: {
  icon?: ReactNode;
  title: ReactNode;
  /** The explanation line(s) and any status, under the title. */
  children?: ReactNode;
  actions?: ReactNode;
  tone?: 'neutral' | 'danger';
}) {
  return (
    <div className="state-block" data-tone={tone}>
      {icon ? <span className="state-icon">{icon}</span> : null}
      <h1>{title}</h1>
      {children}
      {actions ? <div className="state-actions">{actions}</div> : null}
    </div>
  );
}
