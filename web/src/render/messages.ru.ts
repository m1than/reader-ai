// WEB-RENDER interface copy (WEB-UI FR-UI-30): Russian, reached only through the runtime's import().
import type { Translation } from '../ui/i18n';
import type en from './messages.en';

const ru: Translation<typeof en> = {
  'code.copy': 'Копировать',
  'code.copied': 'Скопировано',
  'code.copyLabel': 'Копировать код',
  'code.copiedStatus': 'Код скопирован',
  'code.copyFailed': 'Не удалось скопировать',
  'code.group': 'Код: {name}',
  'output.truncated': 'Вывод обрезан на 1 МиБ.',
  'output.unsupported': 'неподдерживаемый тип вывода: {types}',
};

export default ru;
