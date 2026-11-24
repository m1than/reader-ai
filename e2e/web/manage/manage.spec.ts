// @trace FR-MANAGE-2 FR-MANAGE-3 FR-MANAGE-4 FR-MANAGE-5 FR-MANAGE-6 FR-MANAGE-7 FR-MANAGE-8 FR-MANAGE-9 FR-MANAGE-10 FR-MANAGE-11 INV-MANAGE-2
// [browser] Run on request: `cd web && npm run build && npm run build-storybook && npm run e2e -- manage`. The WEB-UI
// test server serves web/dist under the production CSP and answers the management routes from VEC-MANAGE-1
// (spec/fixtures/WEB-MANAGE/responses.json); `POST /__test__/reset?role=learner` serves the learner.
import AxeBuilder from '@axe-core/playwright';
import { expect, test, type Page, type Request } from '@playwright/test';

const WIDTHS = [1280, 1024, 390];

const writes = (page: Page) => {
  const out: Array<{ method: string; path: string; body: unknown }> = [];
  page.on('request', (r: Request) => {
    const url = new URL(r.url());
    if (!url.pathname.startsWith('/api/') || r.method() === 'GET') return;
    out.push({ method: r.method(), path: url.pathname + url.search, body: r.postData() ? JSON.parse(r.postData()!) : undefined });
  });
  return out;
};
const noOverflow = async (page: Page, what: string) => {
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(overflow, what).toBeLessThanOrEqual(0);
};
const axe = async (page: Page, what: string) => {
  // FR-UI-15's note: the library dialog fades in; wait for its animations before axe (at most 2 s).
  await page.waitForFunction(() => document.getAnimations().every((a) => a.playState !== 'running'), null, { timeout: 2000 }).catch(() => undefined);
  const result = await new AxeBuilder({ page }).analyze();
  const serious = result.violations.filter((v) => v.impact === 'serious' || v.impact === 'critical');
  expect(serious.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(' ')).join(', ')}`), what).toEqual([]);
};
const reset = (page: Page, query = '') => page.request.post(`/__test__/reset${query}`);
const courseMenu = async (page: Page, title: string) => {
  await page.getByRole('button', { name: `Actions for ${title}`, exact: true }).click();
  return page.getByRole('menu');
};

test.afterEach(async ({ page }) => {
  await reset(page);
});

for (const width of WIDTHS) {
  test(`owner course flows at ${width} px: rename, archive with Undo, delete with typed confirmation and Undo`, async ({ page }) => {
    await reset(page);
    await page.setViewportSize({ width, height: 900 });
    const sent = writes(page);
    await page.goto('/');
    await expect(page.getByRole('heading', { level: 2, name: 'Fixture course' })).toBeVisible();
    await noOverflow(page, '/');
    await (await courseMenu(page, 'Fixture course')).getByRole('menuitem', { name: 'Rename' }).click();
    const box = page.getByRole('textbox', { name: 'Course title' });
    await box.fill('Renamed course');
    await box.press('Enter');
    await expect.poll(() => sent.length).toBe(1);
    expect(sent[0]).toEqual({ method: 'POST', path: '/api/courses/c-fixture', body: { version: 3, title: 'Renamed course' } });

    await (await courseMenu(page, 'Another course')).getByRole('menuitem', { name: 'Archive' }).click();
    await expect(page.getByText('Course archived.')).toBeVisible();
    await page.getByRole('button', { name: 'Undo' }).click();
    await expect(page.getByText('Course restored.')).toBeVisible();
    expect(sent.slice(1, 3)).toEqual([
      { method: 'POST', path: '/api/courses/c-other/archive', body: {} },
      { method: 'POST', path: '/api/courses/c-other/unarchive', body: { at: 'previous' } },
    ]);

    await (await courseMenu(page, 'Another course')).getByRole('menuitem', { name: 'Delete…' }).click();
    const dialog = page.getByRole('dialog', { name: 'Delete the course “Another course”?' });
    await expect(dialog).toBeVisible();
    await expect(dialog.getByRole('button', { name: 'Delete' })).toBeDisabled();
    await dialog.getByRole('textbox', { name: 'Type Another course to confirm' }).fill('Another course');
    await axe(page, `delete dialog ${width}`);
    await noOverflow(page, `delete dialog ${width}`);
    await dialog.getByRole('button', { name: 'Delete' }).click();
    await expect(page.getByText('Course deleted.')).toBeVisible();
    await page.getByRole('button', { name: 'Undo' }).click();
    expect(sent.slice(3)).toEqual([
      { method: 'DELETE', path: '/api/courses/c-other', body: { confirm: 'Another course' } },
      { method: 'POST', path: '/api/courses/c-other/restore', body: { at: 'previous' } },
    ]);
  });
}

test('the lesson menu on a row and on the page, and a revert with its request', async ({ page }) => {
  await reset(page);
  const sent = writes(page);
  await page.goto('/');
  const row = page.locator('#course-c-fixture li', { hasText: 'Fixture lesson W01' });
  await row.hover();
  await row.getByRole('button', { name: /^Actions for Fixture lesson W01/ }).click();
  await expect(page.getByRole('menu').getByRole('menuitem', { name: 'Revisions…' })).toBeVisible();
  await page.keyboard.press('Escape');
  await page.goto('/lessons/w01');
  await page.getByRole('button', { name: 'More lesson actions' }).click();
  await page.getByRole('menuitem', { name: 'Revisions…' }).click();
  const dialog = page.getByRole('dialog', { name: 'Revisions' });
  await expect(dialog.getByText('Current')).toBeVisible();
  await dialog.getByRole('button', { name: 'Revert' }).click();
  await page.getByRole('button', { name: 'Make current' }).click();
  await expect.poll(() => sent.length).toBe(1);
  expect(sent[0]).toEqual({ method: 'POST', path: '/api/lessons/w01/revert', body: { sha256: '5555555555555555555555555555555555555555555555555555555555555555' } });
});

for (const width of WIDTHS)
  test(`your sources on /import at ${width} px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    await page.goto('/import');
    const section = page.getByRole('region', { name: 'Manage your sources' });
    // The test server's /api/materials is WEB-PLAN's synthetic list (shared with the plan specs).
    await expect(section.getByText('synthetic-plan')).toBeVisible();
    await noOverflow(page, `/import ${width}`);
  });

for (const scheme of ['light', 'dark'] as const)
  for (const width of WIDTHS)
    test(`settings tabs at ${width} px in ${scheme}`, async ({ page }) => {
      await page.emulateMedia({ colorScheme: scheme });
      await page.setViewportSize({ width, height: 900 });
      await page.goto('/settings');
      await expect(page.getByRole('heading', { level: 1, name: 'Settings' })).toBeVisible();
      for (const name of ['Appearance', 'Account', 'People', 'Instance', 'Data']) {
        await page.getByRole('tab', { name }).click();
        await expect(page.getByRole('tabpanel')).toBeVisible();
        await noOverflow(page, `${name} ${width} ${scheme}`);
        await axe(page, `${name} ${width} ${scheme}`);
      }
    });

test('text size and reading width survive a reload with no flash', async ({ page }) => {
  await page.goto('/settings');
  await page.getByRole('radio', { name: 'Large' }).click();
  await page.getByRole('radio', { name: 'Wide' }).click();
  await page.addInitScript(() => {
    requestAnimationFrame(() => {
      const w = window as unknown as { __first?: { size: string | null; width: string | null } };
      w.__first = { size: document.documentElement.getAttribute('data-text-size'), width: document.documentElement.getAttribute('data-width') };
    });
  });
  await page.goto('/lessons/w01');
  await expect(page.getByRole('heading', { name: 'Digests' })).toBeVisible();
  const first = await page.evaluate(() => (window as unknown as { __first?: unknown }).__first);
  expect(first).toEqual({ size: 'large', width: 'wide' });
  const size = await page.locator('[data-markdown] p').first().evaluate((el) => getComputedStyle(el).fontSize);
  expect(size).toBe('19px');
});

test('delete → restore round trips from Settings → Data', async ({ page }) => {
  const sent = writes(page);
  await page.goto('/settings');
  await page.getByRole('tab', { name: 'Data' }).click();
  const deleted = page.getByRole('region', { name: 'Recently deleted' });
  await deleted.locator('li', { hasText: 'Deleted course' }).getByRole('button', { name: 'Restore' }).click();
  await deleted.locator('li', { hasText: 'Fixture lesson W03: deleted' }).getByRole('button', { name: 'Restore' }).click();
  await deleted.locator('li', { hasText: 'A deleted book' }).getByRole('button', { name: 'Restore' }).click();
  await expect(page.getByText(/1 course came back with it\./)).toBeVisible();
  expect(sent).toEqual([
    { method: 'POST', path: '/api/courses/c-deleted/restore', body: { at: 'first' } },
    { method: 'POST', path: '/api/lessons/w03/restore', body: {} },
    { method: 'POST', path: '/api/materials/up-cccccccccccc/restore', body: {} },
  ]);
});

for (const locale of ['en', 'ru'])
  test(`the language switch on /settings fits 390 px (${locale})`, async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto('/settings');
    await page.getByRole('radio', { name: locale === 'ru' ? 'Русский' : 'English' }).click();
    await expect(page.getByRole('heading', { level: 1 })).toHaveText(locale === 'ru' ? 'Настройки' : 'Settings');
    expect(await page.evaluate(() => document.documentElement.lang)).toBe(locale);
    await noOverflow(page, `settings ${locale}`);
  });

test('the learner walk: no owner control rendered, no owner route called', async ({ page }) => {
  await reset(page, '?role=learner');
  const owner: string[] = [];
  page.on('request', (r) => {
    const p = new URL(r.url()).pathname;
    if (/^\/api\/(courses|materials|auth\/users|settings|audit|compiles)/.test(p)) owner.push(`${r.method()} ${p}`);
  });
  await page.goto('/');
  await expect(page.getByRole('heading', { level: 2, name: 'Fixture course' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Actions for Fixture course', exact: true })).toHaveCount(0);
  await expect(page.getByRole('button', { name: /^Archived/ })).toHaveCount(0);
  await page.goto('/settings');
  await expect(page.getByRole('tab')).toHaveText(['Appearance', 'Account']);
  await page.goto('/import');
  await expect(page.getByRole('heading', { name: 'Not found' })).toBeVisible();
  expect(owner).toEqual([]);
});
