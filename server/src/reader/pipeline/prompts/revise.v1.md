You are revising one lesson of an interactive study course against the source above. The user message holds the
current lesson (`notebook`, in the compact cell form below), the last fact-check report and the last adversarial
review. Fix every unsupported or contradicted claim and every blocking finding: correct the claim from the source,
cite the span that supports it, or remove the claim. Keep everything that was right. Keep every `notes` cell
unchanged, with its id.

Write in the language of the source. Answer with one JSON object and nothing else: the whole revised lesson,

{"cells": [CELL, ...]}

Every CELL has `id`, `kind`, `source` and optionally `sectors`, plus the fields of its kind:
`diagnostic` (`questions`, `routes`), `route` (`source_refs` [{`span`}]), `explain` (`claims` [{`id`, `text`,
`citations`}]), `changed` (`as_of`, `citations`), `erratum` (`source_ref` {`span`}, `correction`, `citations`),
`tradeoff` (`options`, `rejected_because`), `lab` (Python source, `kernel` "python3", `network` "none",
`asserts`), `recall` (`front`, `back`), `notes`, `open` (`status`).

Citations are {"span": "s<k>"} for a source block above, or {"url": "https://...", "retrieved": "YYYY-MM-DD"} for
facts outside the source. Never invent an alias; do not write page numbers or titles.
