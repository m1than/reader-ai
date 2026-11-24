// WEB-UI shared component copy (FR-UI-30): Russian, reached only through the runtime's import().
import type { Translation } from './i18n';
import type en from './messages.en';

const ru: Translation<typeof en> = {
  'theme.label': 'Тема',
  'theme.system': 'Как в системе',
  'theme.light': 'Светлая',
  'theme.dark': 'Тёмная',
};

export default ru;
