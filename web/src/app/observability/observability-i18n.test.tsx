// @trace FR-UI-30 FR-UI-18 FR-UI-33
// WEB-UI FR-UI-30 on the observability page (FR-UI-18, FR-UI-33): in a Russian interface every heading, column head,
// note, control and empty line comes from the `app/observability` catalogue; ids, codes, stage and model names stay as
// received (the declared exceptions).
import { act, cleanup, render, screen, waitFor, within } from '@testing-library/react';
import type { ReactNode } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { readYaml } from '../testing/fixtures';
import { load } from '../testing/load';

afterEach(() => {
  cleanup();
  localStorage.removeItem('reader-ai.locale');
});

const RUN = '10000000-0000-4000-8000-000000000001';
const json = (status: number, body: unknown) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
const summary = (over: Record<string, unknown> = {}): Record<string, unknown> => ({
  days: 7,
  ...readYaml<{ expected: Record<string, unknown> }>('spec/fixtures/SRV-OBS/summary.yaml').expected,
  alerts: [{ code: 'daily_spend_high', subject: 'tutor', value: 2.5, limit: 2.4 }],
  stack: { mlflow: { ok: true, checked_at: '2026-11-25T12:00:00Z' }, rotel: { ok: false, checked_at: '2026-11-25T11:30:00Z' } },
  ...over,
});

const mountRu = async (body: Record<string, unknown>) => {
  const page = await load(() => import('./ObservabilityPage'));
  const api = await load(() => import('./api'));
  const ctx = await load(() => import('../context'));
  const i18n = await load(() => import('../../ui/i18n'));
  const rq = await import('@tanstack/react-query');
  localStorage.setItem('reader-ai.locale', 'ru');
  await i18n.loadAreas(['app/observability']);
  const fetchFn = vi.fn(async () => json(200, body));
  const obs = api.createObsApi({ fetch: fetchFn as unknown as typeof fetch, onUnauthorized: vi.fn() });
  const client = new rq.QueryClient({ defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } } });
  const Provider = i18n.I18nProvider as (p: { children: ReactNode; languages?: readonly string[] }) => ReactNode;
  await act(async () => {
    render(
      <Provider languages={['en-US']}>
        <ctx.ServicesContext.Provider value={{ api: {} as never, notify: vi.fn(), navigateAway: vi.fn(), obs }}>
          <rq.QueryClientProvider client={client}>
            <main id="content" tabIndex={-1}>
              <page.ObservabilityPage />
            </main>
          </rq.QueryClientProvider>
        </ctx.ServicesContext.Provider>
      </Provider>,
    );
  });
};

describe('the observability page in Russian (FR-UI-30)', () => {
  it('translates headings, column heads, notes and controls and keeps ids, stages and models as received', async () => {
    await mountRu(summary());
    expect(await screen.findByRole('heading', { level: 1, name: 'Наблюдаемость' })).toBeTruthy();
    await waitFor(() => expect(screen.getByRole('heading', { name: 'Запуски сборки' })).toBeTruthy());
    for (const name of ['Наставник', 'Сбои', 'Импорт', 'Стоимость уроков', 'Панели', 'Загрузка результатов замера', 'Состояние служб', 'Итоги по курсам'])
      expect(screen.getByRole('heading', { name })).toBeTruthy();
    expect(screen.queryByRole('heading', { name: 'Compile runs' })).toBeNull();
    expect(screen.getByText(/^Последние 7 дней\. Откройте трассу/)).toBeTruthy();
    expect(screen.getByRole('link', { name: 'панели трасс' }).getAttribute('href')).toBe('https://localhost:8443/');

    const lessons = screen.getByRole('region', { name: 'Стоимость уроков' });
    const table = within(lessons).getByRole('table', { name: 'Стоимость уроков' });
    expect(within(table).getByRole('columnheader', { name: 'Всего, USD' }).getAttribute('aria-sort')).toBe('descending');
    expect(within(lessons).getByRole('table', { name: 'Стоимость курсов' })).toBeTruthy();
    expect(within(lessons).getByText(/среди них 1 открытый резерв/)).toBeTruthy();
    const row = within(lessons).getByText('Lesson one').closest('tr')!;
    expect(within(row).getByRole('button', { name: 'Хорошо' })).toBeTruthy();
    const reason = within(row).getByLabelText('Причина') as HTMLSelectElement;
    expect([...reason.options].find((o) => o.value === 'wrong_fact')?.textContent).toBe('ошибка в фактах');

    // Declared exceptions: a run id, a stage value and a model id, an alert code, service names.
    const runs = screen.getByRole('region', { name: 'Запуски сборки' });
    expect(within(runs).getAllByText(RUN).length).toBeGreaterThan(0);
    expect(within(runs).getByText('fact_check')).toBeTruthy();
    expect(within(runs).getAllByText('claude-sonnet-5-5').length).toBeGreaterThan(0);
    expect(within(runs).getAllByRole('columnheader').map((h) => h.textContent)).toEqual(['Шаг', 'Этап', 'Стоимость, USD', 'Секунды', 'Модель', 'Трасса']);
    expect(screen.getByRole('alert').textContent).toBe('daily_spend_high: tutor (2.5 из 2.4)');
    const stack = screen.getByRole('region', { name: 'Состояние служб' });
    expect(within(stack).getByText(/^mlflow: работает/)).toBeTruthy();
    expect(within(stack).getByText(/^rotel: не работает/)).toBeTruthy();
  });

  it('says an empty view in a whole Russian sentence with the plural of days', async () => {
    const body = summary({ compile_runs: [] });
    await mountRu(body);
    const runs = await waitFor(() => screen.getByRole('region', { name: 'Запуски сборки' }));
    expect(within(runs).getByText('За последние 7 дней запусков сборки не было.')).toBeTruthy();
  });
});
