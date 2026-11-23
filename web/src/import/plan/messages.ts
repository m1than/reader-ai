// The planning part's messages, keyed by code (WEB-PLAN FR-IMPORT-5, -6): plain text only, no server text shown.
export const MESSAGES: Record<string, string> = {
  not_found: 'This source is not imported yet.',
  no_text: 'This source has no text layer to plan from.',
  plan_changed: 'The plan changed; check it and confirm again.',
  does_not_fit: 'This plan does not fit. Choose more weeks or more minutes a day.',
  already_started: 'A lesson of this course is already compiled or compiling.',
  compile_running: 'A compile of this lesson is already running.',
  budget_exhausted: 'The monthly budget is used up; the course is created, compile it later.',
  owner_only: 'Only the owner can plan a course.',
  network: 'The server could not be reached. Check the connection and try again.',
  bad_response: 'The server sent an answer this page does not understand.',
  unknown: 'Something went wrong. Try again.',
};

export const message = (code: string): string => MESSAGES[code] ?? MESSAGES.unknown!;
