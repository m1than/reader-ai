// @trace FR-UI-7
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import { load } from './testing/load';

afterEach(cleanup);

describe('notices (FR-UI-7)', () => {
  it('has a fixed catalogue text for every kernel and app code', async () => {
    const { noticeText } = await load(() => import('./notices'));
    expect(noticeText).toBeTypeOf('function');
    const codes = [
      'rejected', 'busy', 'timeout', 'output_truncated', 'kernel_restarted', 'kernel_dead', 'kernel_reset',
      'kernel_unavailable', 'kernel_upstream', 'too_many_kernels', 'connect_failed', 'sign_out_failed', 'network',
    ];
    for (const code of codes) expect(noticeText(code)).toMatch(/\S/);
    // Design pass (FR-UI-7): sentence case with a full stop.
    expect(noticeText('too_many_kernels')).toBe('Too many open lessons; close one or reset the kernel.');
    expect(noticeText('<img src=x onerror=alert(1)>')).toBe(noticeText('unknown'));
  });

  it('shows at most 3 notices and collapses the rest', async () => {
    const { NoticeRegion } = await load(() => import('./notices'));
    expect(NoticeRegion).toBeTypeOf('function');
    const notices = ['timeout', 'busy', 'rejected', 'kernel_reset', 'network'].map((code, i) => ({ id: i, code }));
    render(<NoticeRegion notices={notices} onDismiss={() => undefined} />);
    expect(screen.getAllByRole('status')).toHaveLength(3);
    expect(screen.getByText(/2 more/)).toBeTruthy();
  });
});
