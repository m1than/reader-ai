// App-wide services handed to routes and the lesson view.
import { createContext, useContext, type ComponentType } from 'react';
import type { SignupsApi } from '../accounts/api';
import type { UploadApi } from '../import/api';
import type { PlanApi } from '../import/plan/api';
import type { TutorApi } from '../tutor/api';
import type { Api, LessonSummary } from './api';
import type { ObsApi } from './observability/api';

/** Where a slot component renders: a lesson row or the lesson page. */
export type SlotPlace = 'list' | 'page';

/** Props of the compile slot's components. */
export interface SlotProps {
  lesson: LessonSummary;
  place: SlotPlace;
}

/** The compile slot (WEB-UI FR-UI-6, the shape FR-UI-15 fills): components, rendered for every lesson row and on the
 * not-published page, so a stateful control fits. */
export interface CompileSlot {
  /** Replaces the default status while it renders something; always visible. */
  Status?: ComponentType<SlotProps>;
  /** At the row's end, revealed on hover and focus-within; interactive controls belong here. */
  Action?: ComponentType<SlotProps>;
}

export interface Services {
  api: Api;
  notify: (code: string) => void;
  navigateAway: (url: string) => void;
  /** SRV-TUTOR's client; the lesson view shows the Ask panel only when it is present. */
  tutor?: TutorApi;
  /** SRV-PLAN's client for the /import page (FR-UI-12, WEB-PLAN). */
  plan?: PlanApi;
  /** WEB-ACCOUNTS's client (FR-UI-14): the owner's Sign-ups screen and header count. */
  signups?: SignupsApi;
  /** SRV-OBS's summary client (FR-UI-18): the owner's observability page. */
  obs?: ObsApi;
  /** Rendered for every lesson row and on the lesson page (FR-UI-6); AppRoot's default renders FR-UI-15's
   * CompileControl for the owner. */
  compileSlot?: CompileSlot;
  /** SRV-INGEST's upload client for the /import page's upload sections (FR-UI-13, WEB-IMPORT). */
  upload?: UploadApi;
}

export const ServicesContext = createContext<Services | null>(null);

export const useServices = (): Services => {
  const s = useContext(ServicesContext);
  if (!s) throw new Error('ServicesContext missing');
  return s;
};
