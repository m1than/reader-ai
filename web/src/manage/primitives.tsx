// WEB-MANAGE FR-MANAGE-1 and -2: the four primitives every management item uses. ActionsMenu (the library Menu behind
// a "⋯" icon button), RenameField (the library text field in place of a title), ConfirmDialog (the library Dialog with
// typed confirmation) and UndoToast (an app-owned toast with Undo: the 10 s timer pauses while hovered or focused, as
// FR-UI-7's notices keep their own region instead of the library's toast manager). Text renders as text nodes only.
import { Button } from '@acme/ui/button';
import { Dialog } from '@acme/ui/dialog';
import { Menu } from '@acme/ui/menu';
import { TextField } from '@acme/ui/text-field';
import { useEffect, useId, useRef, useState, type ReactNode, type RefObject } from 'react';
import { ApiError } from '../app/api';
import { textLang, useT } from '../ui/i18n';
import { MoreIcon } from '../ui/icons';
import './manage.css';
import en from './messages.en';
import { cleanTitle, confirmTarget, matchesConfirm, normaliseConfirm, problem, type ObjectKind, type Problem } from './model';

export const useManageT = () => useT('manage', en);

/** A menu item declared as data (FR-MANAGE-1). A link item (`href`) renders as the menu item itself. */
export interface MenuItemSpec {
  label: string;
  onSelect?: () => void;
  href?: string;
  download?: string;
  danger?: boolean;
  ownerOnly?: boolean;
  hidden?: boolean;
}

/** The items in order, owner-only and hidden ones removed, danger items last. */
export const shownItems = (items: readonly MenuItemSpec[], owner: boolean): MenuItemSpec[] => {
  const keep = items.filter((i) => !i.hidden && (owner || !i.ownerOnly));
  return [...keep.filter((i) => !i.danger), ...keep.filter((i) => i.danger)];
};

/** Menu items for a library Menu popup: a separator before the first danger item. */
export function MenuItems({ items, separatorFirst = false }: { items: readonly MenuItemSpec[]; separatorFirst?: boolean }) {
  const firstDanger = items.findIndex((i) => i.danger);
  return (
    <>
      {separatorFirst && items.length > 0 ? <Menu.Separator /> : null}
      {items.map((item, i) => (
        <MenuEntry key={`${item.label}-${i}`} item={item} separator={i === firstDanger && i > 0} />
      ))}
    </>
  );
}

function MenuEntry({ item, separator }: { item: MenuItemSpec; separator: boolean }) {
  const cls = item.danger ? 'menu-danger' : 'menu-item';
  const onClick = item.onSelect ?? (() => undefined);
  return (
    <>
      {separator ? <Menu.Separator /> : null}
      {item.href ? (
        <Menu.Item className={cls} render={<a href={item.href} download={item.download} />} onClick={onClick}>
          {item.label}
        </Menu.Item>
      ) : (
        <Menu.Item className={cls} onClick={onClick}>
          {item.label}
        </Menu.Item>
      )}
    </>
  );
}

export function ActionsMenu({
  label,
  items,
  owner = true,
  triggerRef,
}: {
  label: string;
  items: MenuItemSpec[];
  owner?: boolean;
  triggerRef?: RefObject<HTMLButtonElement | null>;
}) {
  const t = useManageT();
  const shown = shownItems(items, owner);
  if (shown.length === 0) return null;
  return (
    <Menu.Root>
      <Menu.Trigger
        ref={triggerRef}
        className="acme-button icon-button manage-trigger"
        data-variant="ghost"
        data-color="neutral"
        data-size="sm"
        aria-label={t('actions_for', { name: label })}
      >
        <MoreIcon />
      </Menu.Trigger>
      <Menu.Portal>
        <Menu.Positioner align="end">
          <Menu.Popup className="app-menu">
            <MenuItems items={shown} />
          </Menu.Popup>
        </Menu.Positioner>
      </Menu.Portal>
    </Menu.Root>
  );
}

/** One problem line: the fixed copy, and a sign-in link when a recent sign-in is required (FR-MANAGE-1). */
export function ProblemLine({ problem: p, children }: { problem: Problem | null; children?: ReactNode }) {
  const t = useManageT();
  if (!p) return null;
  return (
    <p role="alert" className="manage-problem">
      {p.text}
      {p.signIn ? (
        <>
          {' '}
          <a href={p.signIn}>{t('problem.sign_in')}</a>
        </>
      ) : null}
      {children}
    </p>
  );
}

/** The error code of a thrown request. */
export const codeOf = (e: unknown): string => (e instanceof ApiError ? e.code : 'network');

export function RenameField({
  value,
  label,
  onSave,
  onCancel,
  hint,
  max = 200,
  returnFocus,
  object = 'course',
}: {
  value: string;
  label: string;
  onSave: (v: string) => Promise<void>;
  onCancel: () => void;
  hint?: string;
  max?: number;
  returnFocus?: RefObject<HTMLElement | null>;
  object?: ObjectKind;
}) {
  const t = useManageT();
  const [text, setText] = useState(value);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<{ text: string; reload?: boolean; signIn?: string } | null>(null);
  const done = useRef(false);
  const input = useRef<HTMLInputElement>(null);
  const hintId = useId();
  useEffect(() => input.current?.focus(), []);
  const back = () => window.setTimeout(() => returnFocus?.current?.focus(), 0);
  const cancel = () => {
    done.current = true;
    onCancel();
    back();
  };
  const save = async () => {
    if (saving || done.current) return;
    const clean = cleanTitle(text, max);
    if (!clean.ok) {
      setError({ text: t('title.invalid', { max }) });
      return;
    }
    // An unchanged value sends nothing (FR-MANAGE-1); the field stays until Escape or another value.
    if (clean.value === value.normalize('NFC')) return;
    setSaving(true);
    setError(null);
    try {
      await onSave(clean.value);
      done.current = true;
      back();
    } catch (e) {
      const code = codeOf(e);
      if (code === 'stale') setError({ text: t('problem.stale'), reload: true });
      else if (code === 'compile_running') setError({ text: t('problem.rename_running') });
      else {
        const p = problem(code, object, t);
        setError(p.signIn ? { text: p.text, signIn: p.signIn } : { text: p.text });
      }
    } finally {
      setSaving(false);
    }
  };
  return (
    <span className="rename-field">
      <TextField.Root size="sm" value={text} onValueChange={(v) => setText(v)} readOnly={saving} invalid={error !== null}>
        <TextField.Label className="visually-hidden">{label}</TextField.Label>
        <TextField.Input
          ref={input}
          lang={textLang(text)}
          aria-describedby={hint ? hintId : undefined}
          onChange={(e) => setText(e.currentTarget.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') {
              e.preventDefault();
              void save();
            } else if (e.key === 'Escape') {
              e.preventDefault();
              e.stopPropagation();
              cancel();
            }
          }}
          onBlur={() => {
            if (!done.current && !error && !saving) void save();
          }}
        />
      </TextField.Root>
      {hint ? (
        <span id={hintId} className="muted rename-hint">
          {hint}
        </span>
      ) : null}
      {error ? (
        <span role="alert" className="manage-problem">
          {error.text}
          {error.reload ? (
            <Button variant="ghost" color="neutral" size="sm" onClick={() => window.location.reload()}>
              {t('reload')}
            </Button>
          ) : null}
          {error.signIn ? (
            <>
              {' '}
              <a href={error.signIn}>{t('problem.sign_in')}</a>
            </>
          ) : null}
        </span>
      ) : null}
    </span>
  );
}

export function ConfirmDialog({
  open,
  title,
  children,
  confirmLabel,
  typed,
  onConfirm,
  onClose,
  alert = null,
  extra,
  danger = true,
}: {
  open: boolean;
  title: string;
  children?: ReactNode;
  confirmLabel: string;
  /** A title whose first 40 characters must be typed (FR-MANAGE-2). */
  typed?: string;
  onConfirm: (typed: string | undefined) => Promise<void>;
  onClose: () => void;
  alert?: Problem | string | null;
  /** More actions beside Cancel (FR-MANAGE-2's "Cancel compile"). */
  extra?: ReactNode;
  danger?: boolean;
}) {
  const t = useManageT();
  const [value, setValue] = useState('');
  const [busy, setBusy] = useState(false);
  const cancelRef = useRef<HTMLButtonElement>(null);
  const inputId = useId();
  useEffect(() => {
    if (!open) setValue('');
  }, [open]);
  const target = typed === undefined ? undefined : confirmTarget(typed);
  const ok = typed === undefined || matchesConfirm(value, typed);
  const confirm = async () => {
    if (!ok || busy) return;
    setBusy(true);
    try {
      // FR-MANAGE-2: the request's `confirm` is the typed text, NFC-normalised and trimmed (equal to the target here).
      await onConfirm(typed === undefined ? undefined : normaliseConfirm(value));
    } finally {
      setBusy(false);
    }
  };
  const shownAlert: Problem | null = alert === null ? null : typeof alert === 'string' ? { text: alert } : alert;
  return (
    <Dialog.Root open={open} onOpenChange={(next) => (next ? undefined : onClose())}>
      <Dialog.Portal>
        <Dialog.Backdrop />
        <Dialog.Popup initialFocus={cancelRef} className="manage-dialog">
          <Dialog.Title>{title}</Dialog.Title>
          <div className="manage-dialog-body">{children}</div>
          {target !== undefined ? (
            <p className="confirm-type">
              <label htmlFor={inputId}>{t('confirm.type', { text: target })}</label>
              <input id={inputId} className="manage-input" type="text" lang={textLang(target)} value={value} autoComplete="off" spellCheck={false} onChange={(e) => setValue(e.target.value)} />
            </p>
          ) : null}
          <ProblemLine problem={shownAlert} />
          <div className="dialog-actions">
            <Button ref={cancelRef} variant="outline" color="neutral" onClick={onClose}>
              {t('cancel')}
            </Button>
            {extra}
            <Button color={danger ? 'danger' : 'accent'} disabled={!ok || busy} onClick={() => void confirm()}>
              {confirmLabel}
            </Button>
          </div>
        </Dialog.Popup>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

/** FR-MANAGE-1: "<Object> deleted." with Undo for 10 s, paused while hovered or focused; Undo's result is announced. */
export function UndoToast({ text, restoredText, onUndo, onClose, ms = 10_000 }: { text: string; restoredText: string; onUndo: () => Promise<void>; onClose: () => void; ms?: number }) {
  const t = useManageT();
  const [paused, setPaused] = useState(false);
  const [restored, setRestored] = useState(false);
  const [failed, setFailed] = useState<Problem | null>(null);
  const left = useRef(ms);
  useEffect(() => {
    if (paused || restored) return;
    const started = Date.now();
    const timer = window.setTimeout(onClose, left.current);
    return () => {
      window.clearTimeout(timer);
      left.current = Math.max(0, left.current - (Date.now() - started));
    };
  }, [paused, restored, onClose]);
  useEffect(() => {
    if (!restored) return;
    const timer = window.setTimeout(onClose, 4000);
    return () => window.clearTimeout(timer);
  }, [restored, onClose]);
  return (
    <div
      className="undo-toast"
      onMouseEnter={() => setPaused(true)}
      onMouseLeave={() => setPaused(false)}
      onFocus={() => setPaused(true)}
      onBlur={() => setPaused(false)}
    >
      <span role="status">{restored ? restoredText : text}</span>
      {!restored ? (
        <Button
          variant="ghost"
          color="neutral"
          size="sm"
          onClick={() => {
            void (async () => {
              try {
                await onUndo();
                setRestored(true);
              } catch (e) {
                setFailed(problem(codeOf(e), 'course', t));
              }
            })();
          }}
        >
          {t('undo')}
        </Button>
      ) : null}
      <ProblemLine problem={failed} />
    </div>
  );
}
