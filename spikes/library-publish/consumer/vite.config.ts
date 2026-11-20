import react from '@vitejs/plugin-react';
import { defineConfig } from 'vite';

// `--mode baseline` builds React alone (src/baseline.tsx), so the library's share of the bundle is the difference.
export default defineConfig(({ mode }) => ({
  plugins: [react()],
  build: {
    outDir: mode === 'baseline' ? 'dist-baseline' : 'dist',
    rollupOptions: { input: mode === 'baseline' ? 'baseline.html' : 'index.html' },
  },
}));
