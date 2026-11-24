// WEB-UI FR-UI-30: the Russian catalogue of each area, reached only through import(), so each is a lazy chunk and no
// `messages.ru` module sits in an initial chunk. Registered by the entry (main.tsx) and by the Vitest setup.
import { registerRuLoaders, type RuLoaders } from './i18n';

export const RU_LOADERS: RuLoaders = {
  app: () => import('../app/messages.ru'),
  'app/observability': () => import('../app/observability/messages.ru'),
  ui: () => import('./messages.ru'),
  notebook: () => import('../notebook/messages.ru'),
  render: () => import('../render/messages.ru'),
  tutor: () => import('../tutor/messages.ru'),
  import: () => import('../import/messages.ru'),
  'import/plan': () => import('../import/plan/messages.ru'),
  accounts: () => import('../accounts/messages.ru'),
  fresh: () => import('../fresh/messages.ru'),
  manage: () => import('../manage/messages.ru'),
};

registerRuLoaders(RU_LOADERS);
