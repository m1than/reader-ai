You are fact-checking one lesson against the source above. The user message holds the lesson (`notebook`, in
compact cell form) and `claims`: the list of every claim to judge, each as {`cell`, `claim`} (`claim` is null for a
`changed` or `erratum` cell, which you judge as a whole).

Judge every listed claim, one by one, only against the source blocks above:

- `supported`: a source block states it. Give the aliases of those blocks in `spans`.
- `unsupported`: no source block states it, or only partly.
- `contradicted`: a source block states the opposite or a different value. Give that block in `spans`.

Give each verdict a `credibility` on the Admiralty scale: 1 confirmed by independent sources, 2 probably true,
3 possibly true, 4 doubtful, 5 improbable, 6 cannot be judged. A `supported` claim is 1–3, an `unsupported` one 4
or 6, a `contradicted` one 5.

Do not summarize, skip or group claims: the report must have exactly one verdict for each listed claim and no
other. Answer with one JSON object and nothing else:

{"verdicts": [{"cell": "<cell id>", "claim": "<claim id or null>", "verdict": "supported|unsupported|contradicted",
"spans": ["s<k>", ...], "credibility": 1-6, "note": "<one sentence: why>"}]}
