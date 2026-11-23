// Code blocks (WEB-RENDER FR-RENDER-4): a wide block with a header (the language and Copy), plain monospace first;
// python is highlighted once the lazy Shiki chunk has loaded, as React elements carrying both schemes' colours as
// custom properties, so the block follows the theme without a re-render.
import { Button } from '@acme/ui/button';
import type { Root } from 'hast';
import { useEffect, useRef, useState } from 'react';
import { CopyIcon } from '../ui/icons';
import { hastToReact } from './hast';

const COPIED_MS = 2000; // structural: long enough to read "Copied"

function CopyCode({ code }: { code: string }) {
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
      <Button size="sm" variant="ghost" color="neutral" aria-label="Copy code" data-render-chrome="" onClick={() => void copy()}>
        <CopyIcon />
        <span aria-hidden="true">{state === 'copied' ? 'Copied' : 'Copy'}</span>
      </Button>
      <span role="status" className="visually-hidden" data-render-chrome="">
        {state === 'copied' ? 'Code copied' : state === 'failed' ? 'Copy failed' : ''}
      </span>
    </>
  );
}

export function Code({ code, language }: { code: string; language: string }) {
  const [tree, setTree] = useState<Root | null>(null);
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
  return (
    // The chrome is built from no input; INV-RENDER-1 trusts each element marked `data-render-chrome`.
    <div className="code-block wide" data-render-chrome="">
      <div className="code-header" data-render-chrome="">
        <span className="code-lang" data-render-chrome="">
          {language || 'text'}
        </span>
        <CopyCode code={code} />
      </div>
      {tree ? (
        hastToReact(tree)
      ) : (
        <pre data-code="plain">
          <code>{code}</code>
        </pre>
      )}
    </div>
  );
}
