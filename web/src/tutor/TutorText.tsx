// Answer block text (SRV-TUTOR FR-TUTOR-11, VEC-TUTOR-6): WEB-RENDER's markdown pipeline with links and images
// as plain text, because the text comes from a model.
import { Markdown } from '../render/markdown';

export function TutorText({ text }: { text: string }) {
  return <Markdown source={text} linksAsText />;
}
