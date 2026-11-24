// The user menu (WEB-UI FR-UI-11, FR-UI-16): the theme choice and Sign out in the library Menu. Loaded lazily on the
// trigger's first activation (review B1); from then on this component renders the trigger itself.
import { Menu } from '@acme/ui/menu';
import { useState } from 'react';
import { textLang, useT } from '../ui/i18n';
import uiEn from '../ui/messages.en';
import { THEMES, setTheme, storedTheme, type Theme } from '../ui/theme';
import en from './messages.en';

export function UserMenu({
  displayName,
  defaultOpen = false,
  onSignOut,
}: {
  displayName: string;
  defaultOpen?: boolean;
  onSignOut: () => void;
}) {
  const [theme, setChoice] = useState<Theme>(() => storedTheme());
  const t = useT('app', en);
  const tu = useT('ui', uiEn);
  return (
    <Menu.Root defaultOpen={defaultOpen}>
      <Menu.Trigger
        className="acme-button user-trigger"
        data-variant="ghost"
        data-color="neutral"
        data-size="sm"
        aria-label={t('header.account_menu', { name: displayName })}
      >
        <span className="who" lang={textLang(displayName)}>
          {displayName}
        </span>
      </Menu.Trigger>
      <Menu.Portal>
        <Menu.Positioner align="end">
          <Menu.Popup className="app-menu">
            <Menu.RadioGroup
              value={theme}
              onValueChange={(v: unknown) => {
                const next = (THEMES as readonly unknown[]).includes(v) ? (v as Theme) : 'system';
                setChoice(next);
                setTheme(next);
              }}
            >
              <Menu.GroupLabel>{tu('theme.label')}</Menu.GroupLabel>
              {THEMES.map((th) => (
                <Menu.RadioItem key={th} value={th}>
                  <Menu.RadioItemIndicator className="menu-check" aria-hidden="true" />
                  {tu(`theme.${th}`)}
                </Menu.RadioItem>
              ))}
            </Menu.RadioGroup>
            <Menu.Separator />
            <Menu.Item onClick={onSignOut}>{t('menu.sign_out')}</Menu.Item>
          </Menu.Popup>
        </Menu.Positioner>
      </Menu.Portal>
    </Menu.Root>
  );
}

export default UserMenu;
