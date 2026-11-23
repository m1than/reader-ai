// The user menu (WEB-UI FR-UI-11, FR-UI-16): the theme choice and Sign out in the library Menu. Loaded lazily on the
// trigger's first activation (review B1); from then on this component renders the trigger itself.
import { Menu } from '@acme/ui/menu';
import { useState } from 'react';
import { THEMES, setTheme, storedTheme, type Theme } from '../ui/theme';

const LABEL: Record<Theme, string> = { system: 'System', light: 'Light', dark: 'Dark' };

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
  return (
    <Menu.Root defaultOpen={defaultOpen}>
      <Menu.Trigger
        className="acme-button user-trigger"
        data-variant="ghost"
        data-color="neutral"
        data-size="sm"
        aria-label={`Account menu for ${displayName}`}
      >
        <span className="who">{displayName}</span>
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
              <Menu.GroupLabel>Theme</Menu.GroupLabel>
              {THEMES.map((t) => (
                <Menu.RadioItem key={t} value={t}>
                  <Menu.RadioItemIndicator className="menu-check" aria-hidden="true" />
                  {LABEL[t]}
                </Menu.RadioItem>
              ))}
            </Menu.RadioGroup>
            <Menu.Separator />
            <Menu.Item onClick={onSignOut}>Sign out</Menu.Item>
          </Menu.Popup>
        </Menu.Positioner>
      </Menu.Portal>
    </Menu.Root>
  );
}

export default UserMenu;
