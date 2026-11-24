// @trace FR-UI-31
// [browser] Run on request: `cd web && npm run build && npm run build-storybook && npm run e2e -- ui/i18n`. The test
// server serves web/dist under the production CSP with the WEB-UI and WEB-NOTEBOOK fixtures.
// WEB-UI FR-UI-31: a stored Russian interface is in place before first paint and the first commit never shows the
// English list; a failed Russian catalogue renders English and retries on the next load; both languages fit 390 px.
import { expect, test, type Page } from '@playwright/test';

const RU_LESSONS = 'Уроки'; // web/src/app/messages.ru.ts 'list.title'

const seed = async (page: Page, locale: string) => {
  await page.addInitScript((value) => {
    if (!sessionStorage.getItem('seeded-locale')) {
      localStorage.setItem('reader-ai.locale', value);
      sessionStorage.setItem('seeded-locale', '1');
    }
  }, locale);
};

test('a stored ru sets html lang before first paint and the English "Lessons" h1 never appears', async ({ page }) => {
  await seed(page, 'ru');
  await page.addInitScript(() => {
    const w = window as unknown as { __firstLang?: string; __sawEnglish?: boolean };
    w.__sawEnglish = false;
    requestAnimationFrame(() => {
      w.__firstLang = document.documentElement.lang;
    });
    const check = () => {
      for (const h of document.querySelectorAll('h1')) if (h.textContent === 'Lessons') w.__sawEnglish = true;
    };
    new MutationObserver(check).observe(document, { subtree: true, childList: true, characterData: true });
  });
  await page.goto('/');
  await expect(page.locator('#content h1')).toHaveText(RU_LESSONS);
  const seen = await page.evaluate(() => {
    const w = window as unknown as { __firstLang?: string; __sawEnglish?: boolean };
    return { first: w.__firstLang, english: w.__sawEnglish };
  });
  expect(seen.first).toBe('ru');
  expect(seen.english).toBe(false);
  expect(await page.evaluate(() => document.documentElement.lang)).toBe('ru');
});

test('an aborted ru catalogue renders English with html lang=en, keeps the choice, and the next load is Russian', async ({ page }) => {
  await seed(page, 'ru');
  await page.route(/messages\.ru[^/]*\.js(\?.*)?$/, (route) => route.abort());
  await page.goto('/');
  await expect(page.locator('#content h1')).toHaveText('Lessons');
  expect(await page.evaluate(() => document.documentElement.lang)).toBe('en');
  expect(await page.evaluate(() => localStorage.getItem('reader-ai.locale'))).toBe('ru');
  await page.unrouteAll();
  await page.reload();
  await expect(page.locator('#content h1')).toHaveText(RU_LESSONS);
  expect(await page.evaluate(() => document.documentElement.lang)).toBe('ru');
});

for (const locale of ['en', 'ru']) {
  test(`${locale}: /, a lesson and /settings fit 390 px`, async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await seed(page, locale);
    for (const path of ['/', '/lessons/w01', '/settings']) {
      await page.goto(path);
      await expect(page.locator('#content h1').first()).toBeVisible();
      expect(await page.evaluate(() => document.documentElement.lang), path).toBe(locale);
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
      expect(overflow, path).toBeLessThanOrEqual(0);
    }
  });
}
