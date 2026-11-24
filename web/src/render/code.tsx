// Code blocks (WEB-RENDER FR-RENDER-4): a wide block with a header (the language and Copy), plain monospace first;
// python is highlighted once the lazy Shiki chunk has loaded, as React elements carrying both schemes' colours as
// custom properties, so the block follows the theme without a re-render. The block is a labelled group; its scroller
// is a tab stop only while its lines are wider than it (code review).
import { Button } from '@acme/ui/button';
import type { Root } from 'hast';
import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { useT } from '../ui/i18n';
import { CopyIcon } from '../ui/icons';
import { hastToReact } from './hast';
import en from './messages.en';

const COPIED_MS = 2000; // structural: long enough to read "Copied"

function CopyCode({ code }: { code: string }) {
  const t = useT('render', en);
  const [state, setState] = useState<'idle' | 'copied' | 'failed'>('idle');
  const timer = useRef<number | undefined>(undefined);
  useEffect(() => () => window.clearTimeout(timer.current), []);
  const copy = async () => {
    window.clearTimeout(timer.current);
    try {
      await navigator.clipboard.writeText(code);
      setState('copied');
      timer.current = window.setTimeout(() => setState('idle'), COPIED_MS);
    } catch {
      setState('failed');
    }
  };
  return (
    <>
      <Button size="sm" variant="ghost" color="neutral" aria-label={t('code.copyLabel')} data-render-chrome="" onClick={() => void copy()}>
        <CopyIcon />
        <span aria-hidden="true">{state === 'copied' ? t('code.copied') : t('code.copy')}</span>
      </Button>
      <span role="status" className="visually-hidden" data-render-chrome="">
        {state === 'copied' ? t('code.copiedStatus') : state === 'failed' ? t('code.copyFailed') : ''}
      </span>
    </>
  );
}

/** True while the element's content is wider than the element: on mount, when the content changes, on resize. */
const useOverflow = (content: unknown) => {
  const ref = useRef<HTMLDivElement>(null);
  const [overflows, setOverflows] = useState(false);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return undefined;
    const measure = () => setOverflows(el.scrollWidth > el.clientWidth);
    measure();
    if (typeof ResizeObserver !== 'function') return undefined;
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, [content]);
  return [ref, overflows] as const;
};

export function Code({ code, language }: { code: string; language: string }) {
  const t = useT('render', en);
  const [tree, setTree] = useState<Root | null>(null);
  const [scroller, overflows] = useOverflow(tree ?? code);
  useEffect(() => {
    let live = true;
    setTree(null);
    if (language === 'python') {
      void import('./highlight')
        .then(({ highlight }) => highlight(code, language))
        .then((t) => {
          if (live) setTree(t);
        })
        .catch(() => undefined);
    }
    return () => {
      live = false;
    };
  }, [code, language]);
  const name = language || 'text';
  return (
    // The chrome is built from no input; INV-RENDER-1 trusts each element marked `data-render-chrome`.
    <div className="code-block wide" data-render-chrome="" role="group" aria-label={t('code.group', { name })}>
      {/* WEB-UI FR-UI-32: the header is chrome in the interface language, also inside a lesson's content node. */}
      <div className="code-header" data-render-chrome="" lang={t.lang}>
        <span className="code-lang" data-render-chrome="">
          {name}
        </span>
        <CopyCode code={code} />
      </div>
      {/* Long lines scroll here; a tab stop only while they do (axe scrollable-region-focusable). */}
      <div ref={scroller} className="code-scroll" data-render-chrome="" tabIndex={overflows ? 0 : undefined}>
        {tree ? (
          hastToReact(tree)
        ) : (
          <pre data-code="plain">
            <code>{code}</code>
          </pre>
        )}
      </div>
    </div>
  );
}
