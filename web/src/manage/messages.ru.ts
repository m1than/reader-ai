// WEB-MANAGE interface copy (WEB-UI FR-UI-30): Russian, reached only through the runtime's import().
import type { Translation } from '../ui/i18n';
import type en from './messages.en';

const ru: Translation<typeof en> = {
  'language.label': 'Язык',
};

export default ru;
