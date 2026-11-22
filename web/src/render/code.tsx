// Code blocks (WEB-RENDER FR-RENDER-4): plain monospace first; python is highlighted once the lazy Shiki chunk
// has loaded, as React elements.
import type { Root } from 'hast';
import { useEffect, useState } from 'react';
import { hastToReact } from './hast';

const scheme = (): 'light' | 'dark' =>
  typeof window !== 'undefined' && typeof window.matchMedia === 'function' && window.matchMedia('(prefers-color-scheme: dark)').matches
    ? 'dark'
    : 'light';

export function Code({ code, language }: { code: string; language: string }) {
  const [tree, setTree] = useState<Root | null>(null);
  useEffect(() => {
    let live = true;
    setTree(null);
    if (language === 'python') {
      void import('./highlight')
        .then(({ highlight }) => highlight(code, language, scheme()))
        .then((t) => {
          if (live) setTree(t);
        })
        .catch(() => undefined);
    }
    return () => {
      live = false;
    };
  }, [code, language]);
  if (tree) return <>{hastToReact(tree)}</>;
  return (
    <pre data-code="plain">
      <code>{code}</code>
    </pre>
  );
}
