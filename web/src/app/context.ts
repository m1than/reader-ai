// App-wide services handed to routes and the lesson view.
import { createContext, useContext, type ComponentType } from 'react';
import type { PlanApi } from '../import/plan/api';
import type { TutorApi } from '../tutor/api';
import type { Api, LessonSummary } from './api';

/** The compile slot (WEB-UI FR-UI-6, the shape FR-UI-15 fills): components, rendered for every lesson row and on the
 * not-published page, so a stateful control fits. */
export interface CompileSlot {
  /** Replaces the default status while it renders something; always visible. */
  Status?: ComponentType<{ lesson: LessonSummary }>;
  /** At the row's end, revealed on hover and focus-within; interactive controls belong here. */
  Action?: ComponentType<{ lesson: LessonSummary }>;
}

export interface Services {
  api: Api;
  notify: (code: string) => void;
  navigateAway: (url: string) => void;
  /** SRV-TUTOR's client; the lesson view shows the Ask panel only when it is present. */
  tutor?: TutorApi;
  /** SRV-PLAN's client for the /import page (FR-UI-12, WEB-PLAN). */
  plan?: PlanApi;
  /** Called for every lesson row and the not-published page (FR-UI-6); V/compile-ui provides it. */
  compileSlot?: CompileSlot;
}

export const ServicesContext = createContext<Services | null>(null);

export const useServices = (): Services => {
  const s = useContext(ServicesContext);
  if (!s) throw new Error('ServicesContext missing');
  return s;
};
