// The lazy route page `/materials/{id}/freshness` (WEB-FRESH): the card under its page heading, with a client built
// from the shell's fetch and 401 hook (WEB-UI Services.http).
import { useMemo } from 'react';
import { useServices } from '../app/context';
import { createFreshApi } from './api';
import { FreshnessCard } from './FreshnessCard';

export function FreshnessPage({ materialId }: { materialId: string }) {
  const { http } = useServices();
  const api = useMemo(
    () => createFreshApi(http ?? { fetch: (...a: Parameters<typeof fetch>) => globalThis.fetch(...a), onUnauthorized: () => undefined }),
    [http],
  );
  return (
    <div className="page">
      <h1>Source freshness</h1>
      <FreshnessCard materialId={materialId} api={api} />
    </div>
  );
}
