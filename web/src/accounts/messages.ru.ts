// WEB-ACCOUNTS interface copy (WEB-UI FR-UI-30): Russian, reached only through the runtime's import().
import type { Translation } from '../ui/i18n';
import type en from './messages.en';

const ru: Translation<typeof en> = {
  'problem.reauth_required': 'Чтобы одобрить или отклонить заявку, войдите снова (для этих действий это нужно каждые 10 минут).',
  'problem.not_found': 'Эта заявка больше не ждёт решения.',
  'problem.unreachable': 'Не удалось связаться с reader-ai. Попробуйте снова.',
  'problem.failed': 'Что-то пошло не так. Попробуйте снова.',
  'done.approve': 'Заявка {handle} одобрена.',
  'done.reject': 'Заявка {handle} отклонена.',
  'done.disable': 'Учётная запись {handle} отключена.',
  'confirm.reject': 'Отклонить {handle}? Заявка будет удалена, а имя пользователя освободится.',
  'confirm.disable': 'Отключить {handle}? Все сеансы завершатся, и войти больше не получится. Кнопка «Включить» вернёт доступ.',
  'done.enable': 'Учётная запись {handle} включена.',
  enable: 'Включить',
  'enable.label': 'Включить {handle}',
  requested: 'Заявка подана {when}',
  reject: 'Отклонить',
  disable: 'Отключить',
  cancel: 'Отмена',
  approve: 'Одобрить',
  'approve.label': 'Одобрить {handle}',
  'reject.label': 'Отклонить {handle}',
  'disable.label': 'Отключить {handle}',
  disabled: 'Отключена',
  'sign_in_again': 'Войти снова',
  loading: 'Загружаем заявки',
  title: 'Заявки',
  lead: 'Новые учётные записи ждут здесь, пока вы их не одобрите. Одобренные записи могут входить как ученики.',
  'load_failed': 'Не удалось загрузить заявки.',
  retry: 'Повторить',
  waiting: 'Ждут одобрения',
  'waiting.none': 'Заявок нет.',
  accounts: 'Учётные записи',
  'accounts.none': 'Одобренных учётных записей нет.',
  help: 'Учётная запись, потерявшая ключ доступа, не может его восстановить. Отключите её; человек может зарегистрироваться снова под другим именем.',
};

export default ru;
