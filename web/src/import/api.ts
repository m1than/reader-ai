// Upload routes client (WEB-IMPORT FR-IMPORT-2, -3; SRV-INGEST FR-INGEST-18, -19, -23): the PDF goes up with
// XMLHttpRequest (fetch has no upload progress), the name in X-Reader-Filename (never in the URL); every response is
// validated at run time and errors are ApiError codes.
import * as v from 'valibot';
import { ApiError } from '../app/api';

const State = v.picklist(['queued', 'claimed', 'converting', 'sending', 'importing', 'indexing', 'ready', 'failed']);

const View = v.object({
  id: v.string(),
  material_id: v.string(),
  filename: v.string(),
  size: v.number(),
  language: v.string(),
  state: State,
  code: v.nullable(v.string()),
  pages_done: v.nullable(v.number()),
  pages_total: v.nullable(v.number()),
  attempts: v.number(),
  created_at: v.string(),
  retry: v.nullable(v.picklist(['reconvert', 'requeue'])),
});
export type UploadView = v.InferOutput<typeof View>;
export type UploadState = v.InferOutput<typeof State>;

const Declared = v.object({ material_id: v.string(), upload: v.null() });

const Converter = v.object({
  state: v.picklist(['online', 'offline', 'setup_missing']),
  seen_at: v.nullable(v.string()),
  converter_version: v.nullable(v.string()),
  strays: v.number(),
  busy: v.picklist(['none', 'claim', 'cli_running']),
  stale: v.boolean(),
});
export type ConverterView = v.InferOutput<typeof Converter>;

const Requeued = v.object({ retry: v.picklist(['requeued', 'already_queued']) });

export type Language = 'ru' | 'en' | 'other';
export type UploadResult = { kind: 'upload'; view: UploadView } | { kind: 'declared'; materialId: string };
export type RetryResult = { kind: 'restarted' } | { kind: 'view'; view: UploadView };

export interface UploadApi {
  upload(file: File, language: Language, onProgress: (loaded: number, total: number) => void): { done: Promise<UploadResult>; abort: () => void };
  view(id: string): Promise<UploadView>;
  converter(): Promise<ConverterView>;
  cancel(id: string): Promise<void>;
  discard(id: string): Promise<void>;
  retry(id: string): Promise<RetryResult>;
}

export interface UploadApiDeps {
  fetch: typeof fetch;
  onUnauthorized: () => void;
  xhr?: () => XMLHttpRequest;
}

const errorCode = (status: number, text: string): string => {
  try {
    const body = JSON.parse(text) as { error?: unknown };
    if (typeof body?.error === 'string') return body.error;
  } catch {
    // not JSON: fall through
  }
  return `http_${status}`;
};

const parseUpload = (status: number, text: string): UploadResult => {
  let body: unknown;
  try {
    body = JSON.parse(text);
  } catch {
    throw new ApiError(status, 'bad_response');
  }
  const declared = v.safeParse(Declared, body);
  if (status === 200 && declared.success) return { kind: 'declared', materialId: declared.output.material_id };
  const view = v.safeParse(View, body);
  if (!view.success) throw new ApiError(status, 'bad_response');
  return { kind: 'upload', view: view.output };
};

export const createUploadApi = (deps: UploadApiDeps): UploadApi => {
  const call = async <T>(method: 'GET' | 'POST' | 'DELETE', path: string, schema: v.GenericSchema<unknown, T> | null, ok = [200]): Promise<{ status: number; body: T | null }> => {
    let res: Response;
    try {
      res = await deps.fetch(path, { method, credentials: 'same-origin' });
    } catch {
      throw new ApiError(0, 'network');
    }
    if (res.status === 401) {
      deps.onUnauthorized();
      throw new ApiError(401, 'unauthenticated');
    }
    if (!ok.includes(res.status)) {
      const err = (await res.json().catch(() => null)) as { error?: unknown } | null;
      throw new ApiError(res.status, typeof err?.error === 'string' ? err.error : `http_${res.status}`);
    }
    if (schema === null) return { status: res.status, body: null };
    const parsed = v.safeParse(schema, await res.json().catch(() => undefined));
    if (!parsed.success) throw new ApiError(res.status, 'bad_response');
    return { status: res.status, body: parsed.output };
  };
  const base = '/api/materials/uploads';
  return {
    upload: (file, language, onProgress) => {
      const xhr = deps.xhr ? deps.xhr() : new XMLHttpRequest();
      const done = new Promise<UploadResult>((resolve, reject) => {
        xhr.open('POST', `${base}?${new URLSearchParams({ language })}`);
        xhr.setRequestHeader('Content-Type', 'application/pdf');
        xhr.setRequestHeader('X-Reader-Filename', encodeURIComponent(file.name));
        xhr.upload.onprogress = (e) => {
          if (e.lengthComputable) onProgress(e.loaded, e.total);
        };
        xhr.onerror = () => reject(new ApiError(0, 'network'));
        xhr.onabort = () => reject(new ApiError(0, 'aborted'));
        xhr.onload = () => {
          if (xhr.status === 401) {
            deps.onUnauthorized();
            reject(new ApiError(401, 'unauthenticated'));
            return;
          }
          if (xhr.status !== 200 && xhr.status !== 201) {
            reject(new ApiError(xhr.status, errorCode(xhr.status, xhr.responseText)));
            return;
          }
          try {
            resolve(parseUpload(xhr.status, xhr.responseText));
          } catch (e) {
            reject(e);
          }
        };
        xhr.send(file);
      });
      return { done, abort: () => xhr.abort() };
    },
    view: async (id) => (await call('GET', `${base}/${encodeURIComponent(id)}`, View)).body as UploadView,
    converter: async () => (await call('GET', '/api/materials/converter', Converter)).body as ConverterView,
    cancel: async (id) => {
      await call('POST', `${base}/${encodeURIComponent(id)}/cancel`, null);
    },
    discard: async (id) => {
      await call('DELETE', `${base}/${encodeURIComponent(id)}`, null);
    },
    retry: async (id) => {
      const path = `${base}/${encodeURIComponent(id)}/retry`;
      let res: Response;
      try {
        res = await deps.fetch(path, { method: 'POST', credentials: 'same-origin' });
      } catch {
        throw new ApiError(0, 'network');
      }
      if (res.status === 401) {
        deps.onUnauthorized();
        throw new ApiError(401, 'unauthenticated');
      }
      const body: unknown = await res.json().catch(() => undefined);
      if (res.status === 202 && v.safeParse(Requeued, body).success) return { kind: 'restarted' };
      if (res.status === 200) {
        const view = v.safeParse(View, body);
        if (view.success) return { kind: 'view', view: view.output };
        throw new ApiError(200, 'bad_response');
      }
      const code = (body as { error?: unknown } | undefined)?.error;
      throw new ApiError(res.status, typeof code === 'string' ? code : `http_${res.status}`);
    },
  };
};
