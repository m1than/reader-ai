// Renders a hast tree as React elements (WEB-RENDER design: no HTML string ever reaches the document).
import type { Root } from 'hast';
import { toJsxRuntime, type Components } from 'hast-util-to-jsx-runtime';
import type { ReactNode } from 'react';
import { Fragment, jsx, jsxs } from 'react/jsx-runtime';

export const hastToReact = (tree: Root, components: Partial<Components> = {}): ReactNode =>
  toJsxRuntime(tree, { Fragment, jsx, jsxs, components, ignoreInvalidStyle: true, passKeys: true, passNode: true });
