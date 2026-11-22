import '@acme/ui/styles.css';
import '../src/ui/tokens/calm.css';
import 'katex/dist/katex.min.css';
import '../src/ui/app.css';
import type { Preview } from '@storybook/react-vite';

const preview: Preview = {
  parameters: { layout: 'fullscreen' },
};

export default preview;
