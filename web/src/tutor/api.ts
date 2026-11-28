// Tutor routes client (SRV-TUTOR FR-TUTOR-1, -2): same-origin requests, every response validated at run time.
import * as v from 'valibot';
import { ApiError } from '../app/api';

// SRV-TUTOR FR-TUTOR-8 (ADR-0039 revision): the cited span's stored quote hash; read, never shown; older answers lack it
const Citation = v.object({ span: v.string(), page: v.number(), material_title: v.string(), quote_sha256: v.optional(v.string()) });
const Block = v.object({ text: v.string(), supported: v.boolean(), quote: v.string(), citations: v.array(Citation) });
const Answer = v.object({
  blocks: v.array(Block),
  insufficient: v.boolean(),
  retrieval: v.picklist(['ok', 'degraded']),
  prompt_sha256: v.string(),
  model: v.string(),
});
const State = v.picklist(['queued', 'answered', 'failed', 'refused', 'expired']);
const QuestionStateSchema = v.object({
  state: State,
  answer: v.optional(Answer),
  grounded: v.optional(v.boolean()),
  error_code: v.optional(v.string()),
  // SRV-TUTOR FR-TUTOR-2: the question's own spend, once it is no longer queued (the bench reads it)
  usage: v.optional(v.object({ usd: v.number(), output_tokens: v.number(), thinking_tokens: v.nullable(v.number()) })),
});
const HistoryItemSchema = v.object({
  question_id: v.string(),
  question: v.string(),
  cell_id: v.nullable(v.string()),
  state: State,
  grounded: v.nullable(v.boolean()),
  error_code: v.nullable(v.string()),
  created_at: v.string(),
});
const History = v.object({ questions: v.array(HistoryItemSchema) });
const Asked = v.object({ question_id: v.string() });

export type QuestionState = v.InferOutput<typeof QuestionStateSchema>;
export type HistoryItem = v.InferOutput<typeof HistoryItemSchema>;
export type AnswerBlock = v.InferOutput<typeof Block>;
export interface AskBody {
  question: string;
  cell_id?: string;
  revision?: string;
}

export interface TutorApi {
  ask(lessonId: string, body: AskBody): Promise<{ question_id: string }>;
  getQuestion(lessonId: string, questionId: string): Promise<QuestionState>;
  listQuestions(lessonId: string): Promise<{ questions: HistoryItem[] }>;
}

export interface TutorApiDeps {
  fetch: typeof fetch;
  onUnauthorized: () => void;
}

const seg = encodeURIComponent;

export const createTutorApi = (deps: TutorApiDeps): TutorApi => {
  const call = async <T>(method: 'GET' | 'POST', path: string, schema: v.GenericSchema<unknown, T>, body?: unknown): Promise<T> => {
    const init: RequestInit = { method, credentials: 'same-origin' };
    if (body !== undefined) {
      init.body = JSON.stringify(body);
      init.headers = { 'Content-Type': 'application/json' };
    }
    let res: Response;
    try {
      res = await deps.fetch(path, init);
    } catch {
      throw new ApiError(0, 'network');
    }
    if (res.status === 401) {
      deps.onUnauthorized();
      throw new ApiError(401, 'unauthenticated');
    }
    if (!res.ok) {
      const err = (await res.json().catch(() => null)) as { error?: unknown } | null;
      throw new ApiError(res.status, typeof err?.error === 'string' ? err.error : `http_${res.status}`);
    }
    const parsed = v.safeParse(schema, await res.json().catch(() => undefined));
    if (!parsed.success) throw new ApiError(res.status, 'bad_response');
    return parsed.output;
  };
  return {
    ask: (lessonId, body) => call('POST', `/api/lessons/${seg(lessonId)}/ask`, Asked, body),
    getQuestion: (lessonId, questionId) =>
      call('GET', `/api/lessons/${seg(lessonId)}/questions/${seg(questionId)}`, QuestionStateSchema),
    listQuestions: (lessonId) => call('GET', `/api/lessons/${seg(lessonId)}/questions`, History),
  };
};
