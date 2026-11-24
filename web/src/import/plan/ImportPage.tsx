// The /import page (WEB-UI FR-UI-12; WEB-PLAN FR-IMPORT-8): "Your sources" and, for a chosen imported material,
// the study-time form, the estimate and confirm. The upload sections (WEB-IMPORT) join this page later.
import './plan.css';
import { Button } from '@acme/ui/button';
import { useQuery } from '@tanstack/react-query';
import { useEffect, useRef, useState } from 'react';
import { ApiError } from '../../app/api';
import { useServices } from '../../app/context';
import { useLocale, useT } from '../../ui/i18n';
import type { MaterialRow, PlanApi } from './api';
import en from './messages.en';
import { message, type PlanT } from './messages';
import { PlanSection } from './PlanSection';

/** A status in sentence case, `_` as a space (FR-IMPORT-8, design pass): the fallback for a status the catalogue does
 * not map (WEB-UI FR-UI-30). */
const statusText = (s: string): string => {
  const t = s.replaceAll('_', ' ');
  return t.charAt(0).toUpperCase() + t.slice(1);
};

function SourceRow({ api, material, chosen, onPlan, t }: { api: PlanApi; material: MaterialRow; chosen: boolean; onPlan: () => void; t: PlanT }) {
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
        {imported ? (
          t('sources.pages', { n: String(material.page_count ?? '?') })
        ) : (
          <span className="chip">{t.maybe(`enum.material_status.${material.status}`, undefined, statusText(material.status))}</span>
        )}
      </span>
      {imported ? (
        <Button variant="ghost" color="neutral" size="sm" onClick={onPlan} disabled={stored.isPending}>
          {stored.data ? t('sources.replan') : t('sources.plan')}
        </Button>
      ) : null}
    </li>
  );
}

function Planner({ api, materialId, t }: { api: PlanApi; materialId: string; t: PlanT }) {
  const stored = useQuery({ queryKey: ['plan', 'stored', materialId], queryFn: () => api.stored(materialId) });
  const { formatLocale } = useLocale();
  const heading = useRef<HTMLDivElement>(null);
  useEffect(() => {
    heading.current?.querySelector('h2')?.focus();
  }, [stored.isSuccess]);
  if (stored.isPending)
    return (
      <p role="status" className="muted">
        {t('plan.loading')}
      </p>
    );
  if (stored.isError) return <p className="plan-error">{message(stored.error instanceof ApiError ? stored.error.code : 'unknown', t)}</p>;
  return (
    <div ref={heading} className="plan-wrap">
      <PlanSection key={materialId} api={api} materialId={materialId} stored={stored.data} locale={formatLocale} />
    </div>
  );
}

export function ImportPage() {
  const { plan } = useServices();
  const t = useT('import/plan', en);
  const [chosen, setChosen] = useState<string | null>(null);
  const materials = useQuery({
    queryKey: ['plan', 'materials'],
    queryFn: () => (plan ? plan.materials() : Promise.resolve({ materials: [] })),
  });
  if (!plan) return null;
  return (
    <div className="page import-page">
      <h1>{t('page.title')}</h1>
      <section aria-labelledby="sources-h">
        <h2 id="sources-h">{t('sources.heading')}</h2>
        {materials.isPending ? (
          <p role="status" className="muted">
            {t('sources.loading')}
          </p>
        ) : materials.isError ? (
          <div className="state-actions">
            <p className="plan-error">{message(materials.error instanceof ApiError ? materials.error.code : 'unknown', t)}</p>
            <Button variant="outline" color="neutral" onClick={() => void materials.refetch()}>
              {t('retry')}
            </Button>
          </div>
        ) : materials.data.materials.length === 0 ? (
          <p className="muted">{t('sources.none')}</p>
        ) : (
          <ul className="source-list">
            {materials.data.materials.map((m) => (
              <SourceRow key={m.id} api={plan} material={m} chosen={chosen === m.id} onPlan={() => setChosen(m.id)} t={t} />
            ))}
          </ul>
        )}
      </section>
      {chosen ? <Planner api={plan} materialId={chosen} t={t} /> : null}
    </div>
  );
}
