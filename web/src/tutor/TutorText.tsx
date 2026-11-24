// Answer block text (SRV-TUTOR FR-TUTOR-11, VEC-TUTOR-6): WEB-RENDER's markdown pipeline with links and images
// as plain text, because the text comes from a model. Its `lang` comes from the Cyrillic rule over the text (WEB-UI
// FR-UI-32).
import { Markdown } from '../render/markdown';
import { textLang } from '../ui/i18n';

export function TutorText({ text }: { text: string }) {
  return <Markdown source={text} linksAsText lang={textLang(text)} />;
}
