You are drafting one lesson of an interactive study course from the source above. The learner studies from this
lesson instead of the book chapter, so it must teach everything the source teaches, accurately, and nothing the
source does not support.

Write in the language of the source. Answer with one JSON object and nothing else:

{"cells": [CELL, ...]}

Every CELL has `id` (unique, letters, digits, `-` or `_`, at most 64 characters), `kind`, `source` (Markdown for
the learner, or Python code for a `lab`) and optionally `sectors` (lowercase slugs). Kinds and their extra fields:

- `diagnostic`: `questions` [{`id`, `text`}] and `routes` {route id: [cell ids]}. One at the start.
- `route`: `source_refs` [{`material_sha256` is filled by the system: give only `span`: "s<k>"}].
- `explain`: `claims` [{`id`, `text`, `citations`}]: every factual statement the cell makes is one claim, and each
  claim cites the spans that support it.
- `changed`: `as_of` (YYYY-MM-DD) and `citations`: something that changed after the book was written.
- `erratum`: `source_ref` {`span`: "s<k>"}, `correction`, `citations`: an error in the source.
- `tradeoff`: `options` [{`id`, `name`}] and `rejected_because` {option id: reason}.
- `lab`: Python code with `kernel` "python3", `network` "none" and `asserts` [Python expressions that must be true
  after the cell runs]. Labs run offline, twice, and must give the same output both times. Every lab also has a
  `reference`: where its expected values come from, independently of the lab's own code. One of
  {"kind": "test_vector", "span": "s<k>", "expected": ["<value as written in that block>", ...]} (official test
  vectors in a source block; each expected value appears in an assert), {"kind": "library", "module": "<name>"}
  (a standard-library module the asserts call: hashlib, hmac, decimal, fractions or math), or
  {"kind": "second_implementation"} (the asserts compare against a second, separate implementation).
- `recall`: `front` and `back`: a flash card.
- `notes`: an empty cell for the learner's notes. Include exactly one, last.
- `open`: `status` "open": a question you could not settle from the source.

Citations are {"span": "s<k>"}, naming the source block `s<k>` that supports the claim. Cite only blocks that exist
above; never invent an alias. Use {"url": "https://...", "retrieved": "YYYY-MM-DD"} only for facts that are not in
the source. Do not write page numbers or titles; the system fills them in.
