// WEB-ACCOUNTS FR-ACCOUNTS-1, FR-ACCOUNTS-3: the Sign-ups screen in Light and Dark with 0, 1 and 20 pending.
import type { Decorator, Meta, StoryObj } from '@storybook/react-vite';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { createApi } from '../app/api';
import { ServicesContext } from '../app/context';
import { stubFetch, themed } from '../ui/story';
import { createSignupsApi, type Signups } from './api';
import { SignupsPage } from './SignupsPage';

const one: Signups = {
  pending: [{ id: '6f1d2c3b-4a5e-4f60-8a71-0b2c3d4e5f60', handle: 'lc-1a2b', display_name: 'Анна Петрова', requested_at: '2026-11-23T09:15:00+00:00' }],
  accounts: [
    { id: '7a2e3d4c-5b6f-4071-9b82-1c3d4e5f6071', handle: 'maria', display_name: 'Maria', requested_at: '2026-11-20T08:00:00+00:00', disabled: false },
    { id: '8b3f4e5d-6c70-4182-8c93-2d4e5f607182', handle: 'petr', display_name: 'Petr', requested_at: '2026-11-19T08:00:00+00:00', disabled: true },
  ],
};
const twenty: Signups = {
  pending: Array.from({ length: 20 }, (_, i) => ({
    id: `00000000-0000-4000-8000-${String(i).padStart(12, '0')}`,
    handle: `lc-p${String(i).padStart(2, '0')}`,
    display_name: `Person ${i}`,
    requested_at: `2026-11-23T0${i % 10}:00:00+00:00`,
  })),
  accounts: [],
};

const withSignups =
  (body: Signups): Decorator =>
  (Story) => {
    const deps = { fetch: stubFetch({ '/api/auth/signups': body }), onUnauthorized: () => undefined };
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const services = { api: createApi(deps), signups: createSignupsApi(deps), notify: () => undefined, navigateAway: () => undefined };
    return (
      <ServicesContext.Provider value={services}>
        <QueryClientProvider client={client}>
          <Story />
        </QueryClientProvider>
      </ServicesContext.Provider>
    );
  };

const meta = { title: 'Accounts/SignupsPage', component: SignupsPage } satisfies Meta<typeof SignupsPage>;
export default meta;

type Story = StoryObj<typeof meta>;
export const Light: Story = { decorators: [withSignups(one), themed('light')] };
export const Dark: Story = { decorators: [withSignups(one), themed('dark')] };
export const Empty: Story = { decorators: [withSignups({ pending: [], accounts: [] }), themed('light')] };
export const Twenty: Story = { decorators: [withSignups(twenty), themed('dark')] };
