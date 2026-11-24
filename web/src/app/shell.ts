// What the shell (the root route, WEB-UI FR-UI-11) needs from the gate above the router (AppRoot).
import { createContext } from 'react';

export interface ShellState {
  displayName: string;
  owner: boolean;
  onSignOut: () => void;
  /** The first path was a lesson path with a bad id (checked before routing, since a router may normalise `..`). */
  badFirstPath: boolean;
}

export const ShellContext = createContext<ShellState | null>(null);
