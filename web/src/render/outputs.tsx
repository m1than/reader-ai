// Notebook outputs (WEB-RENDER FR-RENDER-1, FR-RENDER-5, INV-RENDER-2). text/html is never parsed: it renders its
// text/plain alternative or its source as text; SVG renders only as an image through a blob: URL.
import { useEffect, useState } from 'react';
import { Latex } from './latex';
import { Markdown } from './markdown';

export type MimeBundle = Record<string, unknown>;
export type NbOutput =
  | { output_type: 'stream'; name: string; text: string | string[] }
  | { output_type: 'error'; ename: string; evalue: string; traceback: string[] }
  | { output_type: 'display_data' | 'execute_result' | 'update_display_data'; data: MimeBundle; metadata?: unknown; execution_count?: number | null; transient?: unknown };

/** JupyterLab's default rendermime order restricted to the slice's types (VEC-RENDER-1). */
export const PREFERENCE = [
  'text/html', 'text/markdown', 'text/latex', 'image/svg+xml', 'image/png', 'image/jpeg', 'image/gif', 'application/json', 'text/plain',
] as const;

export const TEXT_LIMIT = 1024 * 1024;

export const pickMime = (data: MimeBundle): (typeof PREFERENCE)[number] | null =>
  PREFERENCE.find((m) => Object.hasOwn(data, m)) ?? null;

const joined = (v: unknown): string => (Array.isArray(v) ? v.map(String).join('') : typeof v === 'string' ? v : JSON.stringify(v));

// ESC sequences: CSI (ESC [ … final), OSC (ESC ] … BEL/ST) and two-byte escapes.
// eslint-disable-next-line no-control-regex
const ANSI = /\u001b\[[0-?]*[ -/]*[@-~]|\u001b\][^\u0007\u001b]*(?:\u0007|\u001b\\)|\u001b[@-Z\\-_]/g;
export const stripAnsi = (s: string): string => s.replace(ANSI, '');

function Text({ text, kind = 'text' }: { text: string; kind?: string }) {
  const cut = text.length > TEXT_LIMIT;
  return (
    <div data-output={kind}>
      <pre>{cut ? text.slice(0, TEXT_LIMIT) : text}</pre>
      {cut && <p className="output-note">Output truncated at 1 MiB.</p>}
    </div>
  );
}

const decode = (b64: string): Uint8Array => {
  const bin = atob(b64.replace(/\s+/g, ''));
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
};

function BlobImage({ mime, payload, alt }: { mime: string; payload: string; alt: string | undefined }) {
  const [src, setSrc] = useState<string | null>(null);
  useEffect(() => {
    let url: string | null = null;
    try {
      const bytes = mime === 'image/svg+xml' ? new TextEncoder().encode(payload) : decode(payload);
      url = URL.createObjectURL(new Blob([bytes as BlobPart], { type: mime }));
      setSrc(url);
    } catch {
      setSrc(null);
    }
    return () => {
      if (url) URL.revokeObjectURL(url);
    };
  }, [mime, payload]);
  return <div data-output="image">{src && <img src={src} alt={alt ?? ''} />}</div>;
}

export function Output({ output }: { output: NbOutput }) {
  if (output.output_type === 'stream') return <Text text={stripAnsi(joined(output.text))} />;
  if (output.output_type === 'error') return <Text text={stripAnsi((output.traceback ?? []).join('\n'))} />;
  const data = output.data ?? {};
  const mime = pickMime(data);
  const plain = Object.hasOwn(data, 'text/plain') ? stripAnsi(joined(data['text/plain'])) : undefined;
  switch (mime) {
    case 'text/html':
      return <Text text={plain ?? joined(data['text/html'])} />;
    case 'text/markdown':
      return (
        <div data-output="markdown">
          <Markdown source={joined(data['text/markdown'])} />
        </div>
      );
    case 'text/latex':
      return (
        <div data-output="latex">
          <Latex source={joined(data['text/latex'])} />
        </div>
      );
    case 'image/svg+xml':
    case 'image/png':
    case 'image/jpeg':
    case 'image/gif':
      return <BlobImage mime={mime} payload={joined(data[mime])} alt={plain} />;
    case 'application/json':
      return <Text kind="json" text={JSON.stringify(data['application/json'], null, 2)} />;
    case 'text/plain':
      return <Text text={plain ?? ''} />;
    default:
      return (
        <div data-output="dropped">
          <p>unsupported output type: {Object.keys(data).join(', ')}</p>
        </div>
      );
  }
}
