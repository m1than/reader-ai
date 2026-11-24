// FR-UI-14, WEB-ACCOUNTS FR-ACCOUNTS-4: the Sign-ups nav entry's count, asked once per page load (the screen shares
// the query) and after each action; no polling. Nothing waiting, or a failed call: no badge, the link reads "Sign-ups".
import { useQuery } from '@tanstack/react-query';
import { SIGNUPS_KEY } from '../accounts/api';
import { useServices } from './context';
import type { NavBadge } from './nav';

export const useSignupsBadge = (): NavBadge | null => {
  const { signups } = useServices();
  const q = useQuery({
    queryKey: SIGNUPS_KEY,
    queryFn: () => (signups ? signups.list() : Promise.reject(new Error('no client'))),
    staleTime: Infinity,
    enabled: Boolean(signups),
  });
  const n = q.data?.pending.length ?? 0;
  return n === 0 ? null : { shown: String(n), after: 'waiting' };
};
