// The /import page (WEB-UI FR-UI-12; WEB-PLAN FR-IMPORT-8): "Your sources" and, for a chosen imported material,
// the study-time form, the estimate and confirm. The upload sections (WEB-IMPORT) join this page later.
import './plan.css';
import { Button } from '@acme/ui/button';
import { useQuery } from '@tanstack/react-query';
import { useEffect, useRef, useState } from 'react';
import { ApiError } from '../../app/api';
import { useServices } from '../../app/context';
import type { MaterialRow, PlanApi } from './api';
import { message } from './messages';
import { PlanSection } from './PlanSection';

const locale = () => (typeof navigator !== 'undefined' && navigator.language) || 'en';

/** A status in sentence case, `_` as a space (FR-IMPORT-8, design pass). */
const statusText = (s: string): string => {
  const t = s.replaceAll('_', ' ');
  return t.charAt(0).toUpperCase() + t.slice(1);
};

function SourceRow({ api, material, chosen, onPlan }: { api: PlanApi; material: MaterialRow; chosen: boolean; onPlan: () => void }) {
  const imported = material.status === 'imported';
  const stored = useQuery({
    queryKey: ['plan', 'stored', material.id],
    queryFn: () => api.stored(material.id),
    enabled: imported,
  });
  return (
    <li className="source-row" aria-current={chosen ? 'true' : undefined}>
      <span className="source-title">{material.id}</span>
      <span className="source-meta">
        {imported ? `${material.page_count ?? '?'} pages` : <span className="chip">{statusText(material.status)}</span>}
      </span>
      {imported ? (
        <Button variant="ghost" color="neutral" size="sm" onClick={onPlan} disabled={stored.isPending}>
          {stored.data ? 'Re-plan' : 'Plan study time'}
        </Button>
      ) : null}
    </li>
  );
}

function Planner({ api, materialId }: { api: PlanApi; materialId: string }) {
  const stored = useQuery({ queryKey: ['plan', 'stored', materialId], queryFn: () => api.stored(materialId) });
  const heading = useRef<HTMLDivElement>(null);
  useEffect(() => {
    heading.current?.querySelector('h2')?.focus();
  }, [stored.isSuccess]);
  if (stored.isPending)
    return (
      <p role="status" className="muted">
        Loading the plan
      </p>
    );
  if (stored.isError) return <p className="plan-error">{message(stored.error instanceof ApiError ? stored.error.code : 'unknown')}</p>;
  return (
    <div ref={heading} className="plan-wrap">
      <PlanSection key={materialId} api={api} materialId={materialId} stored={stored.data} locale={locale()} />
    </div>
  );
}

export function ImportPage() {
  const { plan } = useServices();
  const [chosen, setChosen] = useState<string | null>(null);
  const materials = useQuery({
    queryKey: ['plan', 'materials'],
    queryFn: () => (plan ? plan.materials() : Promise.resolve({ materials: [] })),
  });
  if (!plan) return null;
  return (
    <div className="page import-page">
      <h1>Import a source</h1>
      <section aria-labelledby="sources-h">
        <h2 id="sources-h">Your sources</h2>
        {materials.isPending ? (
          <p role="status" className="muted">
            Loading your sources
          </p>
        ) : materials.isError ? (
          <div className="state-actions">
            <p className="plan-error">{message(materials.error instanceof ApiError ? materials.error.code : 'unknown')}</p>
            <Button variant="outline" color="neutral" onClick={() => void materials.refetch()}>
              Retry
            </Button>
          </div>
        ) : materials.data.materials.length === 0 ? (
          <p className="muted">No sources yet.</p>
        ) : (
          <ul className="source-list">
            {materials.data.materials.map((m) => (
              <SourceRow key={m.id} api={plan} material={m} chosen={chosen === m.id} onPlan={() => setChosen(m.id)} />
            ))}
          </ul>
        )}
      </section>
      {chosen ? <Planner api={plan} materialId={chosen} /> : null}
    </div>
  );
}
