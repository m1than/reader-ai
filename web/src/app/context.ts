// App-wide services handed to routes and the lesson view.
import { createContext, useContext, type ReactNode } from 'react';
import type { PlanApi } from '../import/plan/api';
import type { TutorApi } from '../tutor/api';
import type { Api, LessonSummary } from './api';

/** What the compile slot renders for one lesson (WEB-UI FR-UI-6, the shape FR-UI-15 fills). */
export interface CompileSlotParts {
  /** Replaces the default status when non-null; always visible. */
  status: ReactNode | null;
  /** Shown at the row's end, revealed on hover and focus-within. */
  action: ReactNode | null;
}
export type CompileSlot = (lesson: LessonSummary) => CompileSlotParts | null;

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
