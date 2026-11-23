// App-wide services handed to routes and the lesson view.
import { createContext, useContext } from 'react';
import type { SignupsApi } from '../accounts/api';
import type { TutorApi } from '../tutor/api';
import type { Api } from './api';

export interface Services {
  api: Api;
  notify: (code: string) => void;
  navigateAway: (url: string) => void;
  /** SRV-TUTOR's client; the lesson view shows the Ask panel only when it is present. */
  tutor?: TutorApi;
  /** WEB-ACCOUNTS's client (FR-UI-14): the owner's Sign-ups screen and header count. */
  signups?: SignupsApi;
}

export const ServicesContext = createContext<Services | null>(null);

export const useServices = (): Services => {
  const s = useContext(ServicesContext);
  if (!s) throw new Error('ServicesContext missing');
  return s;
};
