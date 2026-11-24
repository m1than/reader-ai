// The upload sections of /import (WEB-IMPORT FR-IMPORT-1..3, -7, -9): a drop zone with one "Choose a PDF" button,
// the upload with a determinate progress bar, then the processing steps polled from SRV-INGEST, the converter's state
// while queued, and Cancel, Retry and Discard as the view allows. The PDF is never read beyond its first 5 bytes,
// never previewed, and no server text is rendered.
import './upload.css';
import { Button } from '@acme/ui/button';
import { useCallback, useEffect, useRef, useState, type DragEvent, type ReactNode } from 'react';
import { ApiError } from '../app/api';
import { interleave, useLocale, useT } from '../ui/i18n';
import type { ConverterView, Language, UploadApi, UploadView } from './api';
import en from './messages.en';
import { message, type ImportT } from './messages';
import { CONVERTER_POLL_MS, POLL_MS, STEPS, checkFile, controls, defaultLanguage, stepOf, terminal } from './model';

const codeOf = (e: unknown): string => (e instanceof ApiError ? e.code : 'unknown');
// The source's language default follows the browser (a content language, not the interface's).
const navLanguage = () => (typeof navigator !== 'undefined' && navigator.language) || 'en';

const LANGUAGES: Language[] = ['ru', 'en', 'other'];

/** A whole sentence with one `{command}` placeholder, the command rendered as code (WEB-UI FR-UI-30). */
const withCommand = (sentence: string, command: string): ReactNode => interleave(sentence, { command: <code>{command}</code> });

// ---- section 1: the drop zone and the upload ----

function UploadZone({ api, onUploaded, locale, t }: { api: UploadApi; onUploaded: (view: UploadView) => void; locale: string; t: ImportT }) {
  const input = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<File | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [language, setLanguage] = useState<Language>(() => defaultLanguage(navLanguage()));
  const [dragging, setDragging] = useState(false);
  const [progress, setProgress] = useState<number | null>(null);
  const [failure, setFailure] = useState<string | null>(null);
  const [declared, setDeclared] = useState(false);
  const abort = useRef<(() => void) | null>(null);

  const choose = async (files: readonly File[]) => {
    setFailure(null);
    setDeclared(false);
    const code = await checkFile(files);
    setProblem(code);
    setFile(code === null ? files[0]! : null);
  };

  const send = async () => {
    if (!file) return;
    setFailure(null);
    setDeclared(false);
    setProgress(0);
    const started = api.upload(file, language, (loaded, total) => setProgress(total > 0 ? loaded / total : 0));
    abort.current = started.abort;
    try {
      const result = await started.done;
      if (result.kind === 'declared') setDeclared(true);
      else onUploaded(result.view);
    } catch (e) {
      const code = codeOf(e);
      if (code !== 'aborted') setFailure(code);
    } finally {
      abort.current = null;
      setProgress(null);
    }
  };

  const onDrop = (e: DragEvent<HTMLElement>) => {
    e.preventDefault();
    setDragging(false);
    void choose(Array.from(e.dataTransfer?.files ?? []));
  };
  const percent = progress === null ? null : new Intl.NumberFormat(locale, { style: 'percent', maximumFractionDigits: 0 }).format(progress);

  return (
    <section
      aria-labelledby="upload-h"
      className={dragging ? 'upload-zone upload-zone-over' : 'upload-zone'}
      onDragOver={(e) => {
        e.preventDefault();
        setDragging(true);
      }}
      onDragLeave={() => setDragging(false)}
      onDrop={onDrop}
    >
      <h2 id="upload-h">{t('upload.heading')}</h2>
      <div className="upload-pick">
        <Button variant="outline" color="neutral" onClick={() => input.current?.click()} disabled={progress !== null}>
          {t('upload.choose')}
        </Button>
        <span className="upload-hint">{t('upload.or_drop')}</span>
        <input
          ref={input}
          type="file"
          accept="application/pdf,.pdf"
          hidden
          onChange={(e) => void choose(Array.from(e.target.files ?? []))}
        />
      </div>
      <p role="status" className="upload-hint">
        {dragging ? t('upload.drop_now') : ''}
      </p>
      {file ? <p className="upload-file">{file.name}</p> : null}
      {problem ? <p className="upload-error">{message(problem, t)}</p> : null}
      <label className="upload-field">
        {t('upload.language')}
        <select value={language} onChange={(e) => setLanguage(e.target.value as Language)} disabled={progress !== null}>
          {LANGUAGES.map((value) => (
            <option key={value} value={value}>
              {t(`language.${value}`)}
            </option>
          ))}
        </select>
      </label>
      {progress !== null ? (
        <div className="upload-progress">
          <div role="progressbar" aria-label={t('upload.uploading')} aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(progress * 100)} className="bar">
            <div className="bar-fill" style={{ inlineSize: `${Math.round(progress * 100)}%` }} />
          </div>
          <span className="upload-hint">{percent}</span>
          <Button variant="outline" color="neutral" size="sm" onClick={() => abort.current?.()}>
            {t('upload.cancel')}
          </Button>
        </div>
      ) : (
        <div className="state-actions">
          <Button onClick={() => void send()} disabled={!file || problem !== null}>
            {t('upload.send')}
          </Button>
          {failure === 'network' ? (
            <Button variant="outline" color="neutral" onClick={() => void send()}>
              {t('upload.retry')}
            </Button>
          ) : null}
        </div>
      )}
      {failure ? <p className="upload-error">{message(failure, t)}</p> : null}
      {declared ? <p className="upload-hint">{t('upload.declared')}</p> : null}
    </section>
  );
}

// ---- section 2: processing ----

const converterLine = (c: ConverterView | null, t: ImportT): ReactNode => {
  if (!c || c.state === 'online') return c?.busy === 'cli_running' ? t('converter.cli_running') : t('converter.waiting');
  if (c.state === 'offline') return withCommand(t('converter.offline'), 'reader up');
  return withCommand(t('converter.unconfigured'), 'reader-ingest --setup');
};

type Ended = 'Cancelled' | 'Discarded' | 'gone' | null;

function Processing({ api, uploadId, onReady, locale, t }: { api: UploadApi; uploadId: string; onReady: (materialId: string) => void; locale: string; t: ImportT }) {
  const [view, setView] = useState<UploadView | null>(null);
  const [converter, setConverter] = useState<ConverterView | null>(null);
  const [ended, setEnded] = useState<Ended>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [visible, setVisible] = useState(() => typeof document === 'undefined' || document.visibilityState !== 'hidden');
  const [paused, setPaused] = useState(false);
  const heading = useRef<HTMLHeadingElement>(null);
  const readied = useRef(false);

  useEffect(() => heading.current?.focus(), []);

  const refresh = useCallback(async () => {
    try {
      const next = await api.view(uploadId);
      setView(next);
      if (next.state === 'ready' && !readied.current) {
        readied.current = true;
        onReady(next.material_id);
      }
    } catch (e) {
      if (e instanceof ApiError && e.status === 404) setEnded((current) => current ?? 'gone');
    }
  }, [api, uploadId, onReady]);

  useEffect(() => {
    const onChange = () => setVisible(document.visibilityState !== 'hidden');
    document.addEventListener('visibilitychange', onChange);
    return () => document.removeEventListener('visibilitychange', onChange);
  }, []);

  // FR-IMPORT-3: the view every 2 s until ready or failed, while the page is visible.
  useEffect(() => {
    if (ended || paused || !visible) return;
    if (view === null) {
      void refresh();
      return;
    }
    if (terminal(view.state)) return;
    const timer = setTimeout(() => void refresh(), POLL_MS);
    return () => clearTimeout(timer);
  }, [view, ended, paused, visible, refresh]);

  // The converter's state: every 10 s while the upload waits; once at first so the warnings show in any state.
  const waiting = view?.state === 'queued';
  useEffect(() => {
    if (ended || !visible) return;
    let stop = false;
    const load = () =>
      api
        .converter()
        .then((c) => !stop && setConverter(c))
        .catch(() => undefined);
    void load();
    if (!waiting) return;
    const timer = setInterval(() => void load(), CONVERTER_POLL_MS);
    return () => {
      stop = true;
      clearInterval(timer);
    };
  }, [api, waiting, ended, visible]);

  const act = async (control: 'Cancel' | 'Retry' | 'Discard') => {
    setNotice(null);
    try {
      if (control === 'Cancel' || control === 'Discard') {
        await (control === 'Cancel' ? api.cancel(uploadId) : api.discard(uploadId));
        setEnded(control === 'Cancel' ? 'Cancelled' : 'Discarded');
        return;
      }
      setPaused(true);
      const result = await api.retry(uploadId);
      if (result.kind === 'restarted') setNotice(t('restarted'));
      else setView(result.view);
    } catch (e) {
      const code = codeOf(e);
      if (code === 'busy') setNotice(message('busy', t));
      else if (code === 'state' || code === 'upload_busy') setView(null);
      else setNotice(message(code, t));
    } finally {
      setConfirming(false);
      setPaused(false);
    }
  };

  const step = view ? stepOf(view.state, view.retry) : 0;
  const number = (n: number) => new Intl.NumberFormat(locale).format(n);
  const allowed = view && !ended ? controls(view) : [];
  const line =
    ended === 'gone'
      ? message('not_found', t)
      : ended
        ? t(`ended.${ended}`)
        : !view
          ? t('processing.loading')
          : view.state === 'failed'
            ? message(view.code ?? 'unknown', t)
            : view.state === 'queued'
              ? null
              : t.enum('upload_state', view.state);
  const stepName = (i: number) => t.maybe(`step.${i}`, undefined, STEPS[i]);

  return (
    <section aria-labelledby="processing-h" className="upload-processing">
      <h2 id="processing-h" ref={heading} tabIndex={-1}>
        {t('processing.heading')}
      </h2>
      {view ? <p className="upload-file">{view.filename}</p> : null}
      <ol role="list" aria-label={t('processing.steps')} className="upload-steps">
        {STEPS.map((name, i) => (
          <li key={name} aria-current={view && !ended && i === step ? 'step' : undefined} className={i < step ? 'done' : undefined}>
            {stepName(i)}
          </li>
        ))}
      </ol>
      <p role="status" aria-label={t('processing.progress')} className="visually-hidden">
        {view && !ended ? stepName(step) : ''}
      </p>
      {view?.state === 'converting' && view.pages_total ? (
        <div className="upload-progress">
          <span className="upload-hint">{t('processing.page', { done: number(view.pages_done ?? 0), total: number(view.pages_total) })}</span>
          <div
            role="progressbar"
            aria-label={t('processing.converting')}
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={Math.round(((view.pages_done ?? 0) / view.pages_total) * 100)}
            className="bar"
          >
            <div className="bar-fill" style={{ inlineSize: `${Math.round(((view.pages_done ?? 0) / view.pages_total) * 100)}%` }} />
          </div>
        </div>
      ) : null}
      {line === null ? (
        <p data-testid="upload-message">{converterLine(converter, t)}</p>
      ) : (
        <p data-testid="upload-message" className={view?.state === 'failed' || ended === 'gone' ? 'upload-error' : undefined}>
          {line}
        </p>
      )}
      {view?.state === 'failed' && view.code === 'memory_limit' && !ended ? <p className="upload-hint">{t('memory_remedy')}</p> : null}
      {converter && converter.strays > 0 ? <p className="upload-warning">{t('converter.strays')}</p> : null}
      {converter?.stale ? <p className="upload-warning">{withCommand(t('converter.stale'), 'reader up')}</p> : null}
      {notice ? <p className="upload-hint">{notice}</p> : null}
      {allowed.length > 0 && !confirming ? (
        <div className="state-actions">
          {allowed.map((control) => (
            <Button
              key={control}
              variant={control === 'Retry' ? 'solid' : 'outline'}
              color={control === 'Retry' ? 'accent' : 'neutral'}
              onClick={() => (control === 'Cancel' ? setConfirming(true) : void act(control))}
            >
              {t(`control.${control}`)}
            </Button>
          ))}
        </div>
      ) : null}
      {confirming ? (
        <div role="dialog" aria-labelledby="cancel-h" aria-describedby="cancel-d" className="upload-confirm">
          <h3 id="cancel-h">{t('confirm.title')}</h3>
          <p id="cancel-d">{t('confirm.body')}</p>
          <div className="state-actions">
            <Button color="danger" onClick={() => void act('Cancel')}>
              {t('confirm.yes')}
            </Button>
            <Button variant="outline" color="neutral" onClick={() => setConfirming(false)}>
              {t('confirm.no')}
            </Button>
          </div>
        </div>
      ) : null}
    </section>
  );
}

export function UploadSections({
  api,
  onReady,
  initialUpload,
  locale: localeProp,
}: {
  api: UploadApi;
  onReady: (materialId: string) => void;
  initialUpload?: string;
  locale?: string;
}) {
  const [uploadId, setUploadId] = useState<string | null>(initialUpload ?? null);
  const t = useT('import', en);
  // Numbers use the formatting locale of the interface language (WEB-UI FR-UI-30).
  const { formatLocale } = useLocale();
  const locale = localeProp ?? formatLocale;
  return (
    <>
      <UploadZone api={api} locale={locale} t={t} onUploaded={(view) => setUploadId(view.id)} />
      {uploadId ? <Processing key={uploadId} api={api} uploadId={uploadId} onReady={onReady} locale={locale} t={t} /> : null}
    </>
  );
}
