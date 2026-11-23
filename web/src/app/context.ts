// App-wide services handed to routes and the lesson view.
import { createContext, useContext } from 'react';
import type { PlanApi } from '../import/plan/api';
import type { TutorApi } from '../tutor/api';
import type { Api } from './api';

export interface Services {
  api: Api;
  notify: (code: string) => void;
  navigateAway: (url: string) => void;
  /** SRV-TUTOR's client; the lesson view shows the Ask panel only when it is present. */
  tutor?: TutorApi;
  /** SRV-PLAN's client for the /import page (FR-UI-12, WEB-PLAN). */
  plan?: PlanApi;
}

export const ServicesContext = createContext<Services | null>(null);

export const useServices = (): Services => {
  const s = useContext(ServicesContext);
  if (!s) throw new Error('ServicesContext missing');
  return s;
};
