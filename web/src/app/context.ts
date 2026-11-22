// App-wide services handed to routes and the lesson view.
import { createContext, useContext } from 'react';
import type { Api } from './api';

export interface Services {
  api: Api;
  notify: (code: string) => void;
  navigateAway: (url: string) => void;
}

export const ServicesContext = createContext<Services | null>(null);

export const useServices = (): Services => {
  const s = useContext(ServicesContext);
  if (!s) throw new Error('ServicesContext missing');
  return s;
};
