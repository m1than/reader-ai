// @trace FR-TUTOR-11
import { describe, expect, it, vi } from 'vitest';
import { ApiError } from '../app/api';
import { createTutorApi } from './api';

const LESSON = '11111111-1111-4111-8111-111111111111';
const QID = '22222222-2222-4222-8222-222222222222';

const respond = (status: number, body: unknown) =>
  Promise.resolve(new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }));

describe('tutor api client', () => {
  it('posts a question and reads question states and the history, validating each response', async () => {
    const fetch = vi.fn((url: string, init?: RequestInit) => {
      if (init?.method === 'POST') return respond(202, { question_id: QID });
      if (url.endsWith('/questions')) return respond(200, { questions: [] });
      return respond(200, { state: 'refused', error_code: 'daily_budget_exhausted' });
    });
    const api = createTutorApi({ fetch: fetch as unknown as typeof globalThis.fetch, onUnauthorized: vi.fn() });
    expect(await api.ask(LESSON, { question: 'q', cell_id: 'c1' })).toEqual({ question_id: QID });
    expect(fetch.mock.calls[0]![0]).toBe(`/api/lessons/${LESSON}/ask`);
    expect(JSON.parse(String(fetch.mock.calls[0]![1]!.body))).toEqual({ question: 'q', cell_id: 'c1' });
    expect(await api.getQuestion(LESSON, QID)).toEqual({ state: 'refused', error_code: 'daily_budget_exhausted' });
    expect(fetch.mock.calls[1]![0]).toBe(`/api/lessons/${LESSON}/questions/${QID}`);
    expect(await api.listQuestions(LESSON)).toEqual({ questions: [] });
  });

  it('maps error bodies to ApiError codes, 401 to the sign-in hook, and refuses malformed responses', async () => {
    const onUnauthorized = vi.fn();
    const bodies: Array<[number, unknown]> = [
      [409, { error: 'tutor_busy' }],
      [401, { error: 'unauthenticated' }],
      [200, { state: 'answered', answer: { blocks: 'nope' } }],
      [200, { state: 'weird' }],
    ];
    const fetch = vi.fn(() => {
      const [status, body] = bodies.shift()!;
      return respond(status, body);
    });
    const api = createTutorApi({ fetch: fetch as unknown as typeof globalThis.fetch, onUnauthorized });
    await expect(api.ask(LESSON, { question: 'q' })).rejects.toEqual(new ApiError(409, 'tutor_busy'));
    await expect(api.getQuestion(LESSON, QID)).rejects.toEqual(new ApiError(401, 'unauthenticated'));
    expect(onUnauthorized).toHaveBeenCalledTimes(1);
    await expect(api.getQuestion(LESSON, QID)).rejects.toEqual(new ApiError(200, 'bad_response'));
    await expect(api.getQuestion(LESSON, QID)).rejects.toEqual(new ApiError(200, 'bad_response'));
  });
});
