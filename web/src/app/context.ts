// App-wide services handed to routes and the lesson view.
import { createContext, useContext } from 'react';
import type { TutorApi } from '../tutor/api';
import type { Api, ApiDeps } from './api';

export interface Services {
  api: Api;
  notify: (code: string) => void;
  navigateAway: (url: string) => void;
  /** SRV-TUTOR's client; the lesson view shows the Ask panel only when it is present. */
  tutor?: TutorApi;
  /** The shell's fetch and 401 hook, for clients built inside lazy chunks (WEB-FRESH's card). */
  http?: ApiDeps;
}

export const ServicesContext = createContext<Services | null>(null);

export const useServices = (): Services => {
  const s = useContext(ServicesContext);
  if (!s) throw new Error('ServicesContext missing');
  return s;
};
