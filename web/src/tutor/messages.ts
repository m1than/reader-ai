// The tutor error catalogue (SRV-TUTOR `errors`, VEC-TUTOR-5): every code has one fixed message; unknown codes
// show tutor_failed's.
const MESSAGES: ReadonlyMap<string, string> = new Map([
  ['question_invalid', 'Please type a question (up to 1000 characters).'],
  ['cell_unknown', 'That cell is no longer in this lesson. Reload the lesson.'],
  ['unknown_revision', 'This lesson changed. Reload it to ask about the current version.'],
  ['not_found', 'This lesson or question is not available.'],
  ['tutor_busy', 'The tutor is still answering your previous question.'],
  ['daily_budget_exhausted', "Today's tutor budget is used up. Try again tomorrow."],
  ['budget_exhausted', 'This question reached its cost limit and was not answered.'],
  ['monthly_budget_exhausted', "This month's AI budget is used up. The tutor is paused until next month."],
  ['provider_missing', 'No AI provider is set up for your account. Ask the owner to connect one.'],
  ['sources_unavailable', 'No indexed source for this lesson yet, so the tutor cannot answer from the book.'],
  ['output_invalid', "The tutor's answer could not be read. Try asking again."],
  ['output_truncated', 'The answer was too long and was cut off. Try a narrower question.'],
  ['provider_unavailable', 'The tutor is unavailable right now. Try again in a minute.'],
  ['rate_limited', 'The tutor is busy right now. Try again in a minute.'],
  ['expired', 'This question waited too long and was not answered. Ask again.'],
  ['lost', 'This question was lost before it was answered. Ask again.'],
  ['tutor_failed', 'The tutor could not answer this question. Ask again.'],
]);

export const CODES: readonly string[] = [...MESSAGES.keys()];

export const messageFor = (code: string): string => MESSAGES.get(code) ?? MESSAGES.get('tutor_failed')!;
