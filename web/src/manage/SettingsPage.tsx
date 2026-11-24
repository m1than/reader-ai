// WEB-MANAGE FR-MANAGE-6..8, -10, -11: the settings page, `/settings`, for every signed-in user. Library tabs:
// Appearance and Account for everyone; People, Instance and Data for the owner only (INV-MANAGE-2: no owner-only
// read runs for anyone else).
import { RadioGroup } from '@acme/ui/radio-group';
import { Tabs } from '@acme/ui/tabs';
import { useId, useState, type ReactNode } from 'react';
import { useLocale, useT, type Choice } from '../ui/i18n';
import uiEn from '../ui/messages.en';
import { setTheme, storedTheme, THEMES, type Theme } from '../ui/theme';
import { AccountTab } from './AccountTab';
import { choosePref, PREFS, readPref } from './appearance';
import { DataTab } from './DataTab';
import { useOwner } from './hooks';
import { InstanceTab } from './InstanceTab';
import { PeopleTab } from './PeopleTab';
import { useManageT } from './primitives';

type Tab = 'appearance' | 'account' | 'people' | 'instance' | 'data';

export function SettingsPage() {
  const t = useManageT();
  const owner = useOwner();
  const [tab, setTab] = useState<Tab>('appearance');
  const tabs: Tab[] = owner ? ['appearance', 'account', 'people', 'instance', 'data'] : ['appearance', 'account'];
  const panel: Record<Tab, () => ReactNode> = {
    appearance: () => <Appearance />,
    account: () => <AccountTab />,
    people: () => <PeopleTab />,
    instance: () => <InstanceTab />,
    data: () => <DataTab />,
  };
  return (
    <div className="page settings-page">
      <h1>{t('settings.title')}</h1>
      <Tabs.Root value={tab} onValueChange={(v) => setTab(v as Tab)} className="settings-tabs">
        <Tabs.List className="settings-tab-list">
          {tabs.map((k) => (
            <Tabs.Tab key={k} value={k} className="settings-tab">
              {t(`tab.${k}`)}
            </Tabs.Tab>
          ))}
        </Tabs.List>
        {tabs.map((k) => (
          <Tabs.Panel key={k} value={k} className="settings-panel">
            {tab === k ? panel[k]() : null}
          </Tabs.Panel>
        ))}
      </Tabs.Root>
    </div>
  );
}

function Choices<V extends string>({
  label,
  value,
  options,
  onChange,
}: {
  label: string;
  value: V;
  options: ReadonlyArray<{ value: V; text: string; lang?: string }>;
  onChange: (v: V) => void;
}) {
  // useId, not the label: a Russian label has no ASCII word characters, so every group got the same id.
  const id = useId();
  return (
    <div className="settings-choice">
      <h2 id={id}>{label}</h2>
      {/* The library's parts as designed: an 18 px Item (the control) and its Label beside it, never text inside the
          Item, whose box is fixed at 18 px (the live check's labels overlaid the controls). */}
      <RadioGroup.Root aria-labelledby={id} value={value} onValueChange={(v) => onChange(v as V)} orientation="horizontal">
        {options.map((o) => {
          const item = `${id}-${o.value}`;
          return (
            <span key={o.value} className="radio-option" lang={o.lang}>
              <RadioGroup.Item id={item} value={o.value}>
                <RadioGroup.Indicator />
              </RadioGroup.Item>
              <RadioGroup.Label htmlFor={item} className="radio-label">
                {o.text}
              </RadioGroup.Label>
            </span>
          );
        })}
      </RadioGroup.Root>
    </div>
  );
}

function Appearance() {
  const t = useManageT();
  const tu = useT('ui', uiEn);
  const { choice, setChoice } = useLocale();
  const [theme, setThemeState] = useState<Theme>(() => storedTheme());
  const [size, setSize] = useState<string>(() => readPref(PREFS.size));
  const [width, setWidth] = useState<string>(() => readPref(PREFS.width));
  return (
    <div className="settings-section">
      <p className="muted">{t('appearance.note')}</p>
      <Choices
        label={tu('theme.label')}
        value={theme}
        options={THEMES.map((v) => ({ value: v, text: tu(`theme.${v}`) }))}
        onChange={(v) => {
          setThemeState(v);
          setTheme(v);
        }}
      />
      <Choices
        label={t('size.label')}
        value={size}
        options={PREFS.size.values.map((v) => ({ value: v, text: t(`size.${v}`) }))}
        onChange={(v) => {
          setSize(v);
          choosePref(PREFS.size, v);
        }}
      />
      <Choices
        label={t('width.label')}
        value={width}
        options={PREFS.width.values.map((v) => ({ value: v, text: t(`width.${v}`) }))}
        onChange={(v) => {
          setWidth(v);
          choosePref(PREFS.width, v);
        }}
      />
      {/* FR-MANAGE-10: "English" and "Русский" carry their own lang whatever the interface language. */}
      <Choices<Choice>
        label={t('language.label')}
        value={choice}
        options={[
          { value: 'auto', text: t('language.auto') },
          { value: 'en', text: t('language.en'), lang: 'en' },
          { value: 'ru', text: t('language.ru'), lang: 'ru' },
        ]}
        onChange={(v) => void setChoice(v)}
      />
    </div>
  );
}
