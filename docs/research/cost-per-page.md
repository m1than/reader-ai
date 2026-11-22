# Research: lowering the total cost per page (2026-11-22)

Question from the owner (2026-11-22): what do recent papers (arXiv) and other sources say about lowering the total cost per page of turning a book PDF into verified interactive lessons? This note feeds V.cost-optimize (`docs/progress.yaml`, levers 1–7).

Sources were read on 2026-11-22. Provider prices and SDK behaviour change often, so every dollar figure below is tied to the price table it uses (see "Caveats"). No book text appears here; all token counts come from `spikes/compile-cost/results/*/[1-6]-*.json`.

## Where the money goes today

| Step | Measured | API $ per page |
| --- | --- | --- |
| Ingest (Docling, native, formula model on) | 15.7–15.9 pages/min; about 11.5 s per formula natively vs 266–284 s in Docker | $0 (local CPU) |
| Embeddings (bge-m3, 8 CPUs) | 22–36 min for the 890-page book; dense recall@10 0.675 on questions-v2 | $0 (local CPU) |
| Compile of L02 (25 pages; draft, fact-check, review, then revise, fact-check, review) | $2.97 cold (run 1), $2.42 with a warm draft cache (run 2, about $2.66 cold-equivalent) | **$0.097–0.119** |

**API spend is entirely the compile.** Ingest and embedding cost wall time and CPU, not dollars. They matter for latency, not for $/page.

Breakdown of run 1 ($2.95 recomputed at `spec/fixtures/SRV-AGENT/pricing.yaml` rates; the SDK reported $2.97):

| Item | $ | Share |
| --- | --- | --- |
| 1-hour cache writes, 251k tokens, almost none re-read | 1.65 | 56 % |
| Output tokens, 70k (revise alone: 25.8k tokens, $0.52) | 1.30 | 44 % |
| Cache reads (only the 6.8k-token CLI system prompt) | 0.01 | 0 % |

By stage group: the first pass (stages 1–3) costs $0.95–1.34, and the single revision loop (stages 4–6) costs $1.47–1.63. **The loop is the most expensive part, and in both runs it did not improve the lesson:** blocking findings went 3 → 3 and 2 → 3.

## Executive summary: top 5 levers for our pipeline

All estimates are recomputed from the measured token counts of runs 1 and 2 at current list prices (Opus 5.5 $4/$20, Sonnet 5.5 $2/$10, Haiku 5.5 $0.10/$0.50 per MTok; 5-minute write 1.25×, 1-hour write 2×, read 0.05× on Opus and Sonnet 5.5). Savings per page use L02's 25 pages and the baseline of $0.097–0.119/page.

| # | Lever | Saving per lesson (L02) | Saving per page | Confidence | Quality risk | How we measure it |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | **Shared, cached source prefix plus a 5-minute TTL.** Put the identical source (and shared instructions) in the system prompt, and move stage instructions and artifacts into the user message. Then every later stage on the same model reads the source at 0.05×. Use the 5-minute TTL (1.25×) instead of the 1-hour TTL (2×) for the single-use tail. | **$0.96–1.02** (2.95 → 1.93; 2.40 → 1.69 cold-equivalent) | **$0.038–0.041** | High: pure arithmetic on provider rules | Low. The content is unchanged, only reordered; check that outputs do not shift | `cache_read_input_tokens` > 0 on stages 2–6; tokens and $ per stage; claim counts and fact-check verdicts stay within the run-to-run variance |
| 2 | **Gate the revision loop.** Loop only on blocking findings that survive a check against the source; re-check only the changed cells (delta loop). | $0 – $1.6, depending on how often the loop is skipped; about **$0.75** expected if half of the lessons skip it | $0 – 0.065 (about **$0.03** expected) | Low: depends on owner ask (a) in `spikes/compile-cost/RESULT.md` | Medium. A real blocking issue could reach G2 | Share of lessons that converge without a loop; G2 return rate; the number of blocking findings the owner confirms at G2 |
| 3 | **Fact-check on Claude Haiku 5.5** instead of Sonnet 5.5 (20× cheaper per token), with an itemized verdict per claim | **$0.43–0.45** standalone; $0.26 after lever 1 | **$0.017–0.018** standalone; $0.010 after lever 1 | Medium: price is certain, quality is not | **High.** Sonnet already rubber-stamped once (87/87 in a summary-only report) | Recall on a seeded-error fixture ≥ 0.9 and ≥ Sonnet's; claim-level agreement with the Sonnet baseline, Cohen's κ ≥ 0.7 |
| 4 | **Patch-based revise.** The revise stage emits only changed or new cells as patches, which the server applies, instead of re-emitting the whole notebook | **$0.12–0.34** (30–65 % of revise output; revise output is 20–26k tokens, $0.40–0.52) | **$0.005–0.013** | Medium: code-editing papers report > 30 % savings; notebooks are untested | Low to medium. A malformed patch must fail closed; apply patches with a strict schema | Revise output tokens; share of patches that apply cleanly (target 100 %); fact-check and review results on the patched notebook |
| 5 | **Review on Sonnet 5.5, or Opus at lower effort,** only where review findings agree with Opus | **$0.20–0.26** after lever 1 | **$0.008–0.010** | Low: no source covers our review task | High. Review is the stage that finds the blocking problems | Overlap of blocking findings with the Opus review (recall of Opus's blocking findings ≥ 0.9); owner spot-check |

**Stacked estimate** (levers 1, 3, 4 and 5, one loop): about $1.17 per L02, or **≈ $0.047/page instead of $0.097–0.119 (−50 to −60 %)**. With lever 2 at the expected 50 % skip rate: **≈ $0.035/page**. These are estimates, not measurements. The experiment plan below measures them.

**The owner's RAG idea (lever 7) is not a cost lever once lever 1 is in place.** At chapter scale the source is about 24k tokens. Once cached, reading it costs $0.0048 per Opus stage, $0.0024 per Sonnet stage and $0.0003 per Haiku stage, so replacing it with retrieved spans saves at most about $0.02 per lesson (< $0.001/page). Retrieval with the evidence for 80 claims at top-3 of about 300-token chunks would even be larger than the chapter itself (about 72k tokens). RAG remains useful for two other things:
- **as a quality lever:** LaRA finds RAG better than long context on hallucination detection (see topic 1);
- **for cross-chapter evidence,** which the chapter alone cannot provide.

It is worth an A/B on quality, not on cost (experiment E4).

**Not recommended now:**
- **Batch API (−50 %):** not reachable through the Agent SDK on the subscription (topic 3).
- **Prompt compression (LLMLingua family):** at most 5 % of the source's cost is left after lever 1, and the method is unvalidated on Russian text and formulas (topic 6).
- **Cheaper embedders:** $0 of API cost per page (topic 7).

## Per-topic findings

### 1. Long context vs RAG

| Source | Date | Key numbers |
| --- | --- | --- |
| Li et al., "Retrieval Augmented Generation or Long-Context LLMs? A Comprehensive Study and Hybrid Approach", https://arxiv.org/abs/2407.16833 (HTML v2: https://arxiv.org/html/2407.16833v2) | 2024-07-23, rev. 2024-10-17 | RAG and long context (LC) give the same prediction on 63 % of queries. Average scores: Gemini-1.5-Pro LC 49.70, RAG 37.33, Self-Route 46.41; GPT-4o LC 48.67, RAG 32.60, Self-Route 48.89. Self-Route uses 38–61 % of LC's tokens, a cost reduction of 65 % (Gemini) and 39 % (GPT-4o). RAG used top-5 chunks of about 300 words. It fails on multi-step, general, long-complex and implicit queries |
| Li et al., "LaRA: Benchmarking Retrieval-Augmented Generation and Long-Context LLMs – No Silver Bullet for LC or RAG Routing", https://arxiv.org/abs/2502.09977 (ICML 2025, https://proceedings.mlr.press/v267/li25dv.html) | 2025-02-14 | 2,326 cases, 11 models. For strong models, LC beats RAG overall by 4.1–9.7 points. On comparison tasks the gap is +14.9 to +33.1 points. On **hallucination detection, RAG wins by 3.5–12.4 points** (Claude-3.5-Sonnet −4.9 at 32k, −7.1 at 128k). RAG setup: 600-token chunks, top-5, hybrid embedding + BM25 |
| Li, Cao, Ma, Sun, "Long Context vs. RAG for LLMs: An Evaluation and Revisits", https://arxiv.org/abs/2501.01880 | 2024-12-27 | LC generally beats RAG on QA. Summarization-based retrieval is about on par with LC; chunk-based retrieval trails both |
| Yu, Xu, Akkiraju, "In Defense of RAG in the Era of Long-Context Language Models" (OP-RAG), https://arxiv.org/abs/2409.01666 | 2024-09-03 | Order-preserving RAG beats full-context with far fewer tokens. Quality follows an inverted U in the number of chunks. Exact F1 figures were not verified from the abstract |

**For us:**
- **The draft must keep the full chapter.** LC wins on comparison and reasoning, which is what a lesson draft does. Our dense recall@10 is only 0.675, so top-k retrieval would drop topics.
- **Fact-check is the one stage where retrieval could help quality.** LaRA's hallucination-detection result points this way. A cheap form is "claim-local" evidence: each claim is checked against its cited page ±1 instead of the whole chapter. This is a quality experiment, since the cost difference after lever 1 is negligible.
- **Self-Route's saving comes from avoiding long-context input.** Prefix caching already makes our long-context input cost 0.05×, so the routing saving shrinks to almost nothing.

### 2. Prompt caching for a multi-stage pipeline

| Source | Date | Key facts |
| --- | --- | --- |
| Anthropic, Prompt caching, https://platform.claude.com/docs/en/build-with-claude/prompt-caching | read 2026-11-22 | Write 1.25× (5 min) or 2× (1 h); read 0.1×, but **0.05× on Opus 5.5 and Sonnet 5.5**. Minimum cacheable prompt is 512 tokens on Opus 5.5, Sonnet 5.5 and Haiku 5.5. At most 4 breakpoints. Prefix order is `tools` → `system` → `messages`. Each breakpoint writes one entry for the prefix that ends there; lookback is 20 blocks and finds only entries that earlier requests wrote. The TTL counts from the start of the request and is refreshed by every hit. 1-hour entries must precede 5-minute entries |
| Anthropic, Pricing, https://platform.claude.com/docs/en/about-claude/pricing | read 2026-11-22 | Opus 5.5 $4 / $5 / $8 / $0.20 / $20 (input, 5-minute write, 1-hour write, read, output). Sonnet 5.5 $2 / $2.50 / $4 / $0.10 / $10. Haiku 5.5 $0.10 / $0.125 / $0.20 / $0.01 / $0.50 for prompts ≤ 100k tokens, and 5× that above 100k. "5-minute write pays off after one read; 1-hour after two." Caching and batch discounts stack |
| Claude Code docs, "How Claude Code uses prompt caching", https://code.claude.com/docs/en/prompt-caching | read 2026-11-22 | **Each model has its own cache.** On a subscription within plan usage, the main conversation (including Agent SDK turns) defaults to the 1-hour TTL, which explains our 100 % `ephemeral_1h` writes. Override with `CLAUDE_CODE_PROMPT_CACHE_TTL=5m`, the `promptCacheTtl` setting, or `FORCE_PROMPT_CACHING_5M=1` |
| Claude Code docs, "Modifying system prompts", https://code.claude.com/docs/en/agent-sdk/modifying-system-prompts.md | read 2026-11-22 | The system prompt is cached as its own layer. With the preset, `exclude_dynamic_sections` makes it identical across machines. The TypeScript SDK can split a custom prompt into a static and a dynamic block (`SYSTEM_PROMPT_DYNAMIC_BOUNDARY`), each with its own breakpoint; Python cannot. The CLI flag `--system-prompt-file` accepts a `__SYSTEM_PROMPT_DYNAMIC_BOUNDARY__` line (Claude Code ≥ 2.1.275) |
| Claude Code docs, "Track cost and usage", https://code.claude.com/docs/en/agent-sdk/cost-tracking.md | read 2026-11-22 | `total_cost_usd` is a client-side estimate from a bundled price table, not billing data. Read output tokens from the result message: per-step `output_tokens` is a placeholder |
| Lumer et al., "Don't Break the Cache: An Evaluation of Prompt Caching for Long-Horizon Agentic Tasks", https://arxiv.org/abs/2601.06007 | 2026-01 (v2) | 41–80 % cost savings across providers. Caching only the system prompt was the most consistent strategy, and full-context caching can raise latency |
| "Prefix Stability for Prompt Caching in LLM Agents: When It Helps and When It Backfires", NeurIPS 2026, https://neurips.cc/virtual/2026/179923 | 2026 | Reports a 24.6 % cost increase when caching is misapplied. Not read beyond the listing |

**Why the spike re-wrote the source in every stage.** Each stage put its instructions before the source. Each single-turn query then wrote one 1-hour entry ending at the end of its own prompt, and no other stage shared that prefix.

**What to do instead:**
- Make the system prompt per model identical: fixed pipeline instructions plus the source.
- Put stage instructions, the notebook and the reports in the user message.
- Opus then writes the prefix once (draft) and reads it in review, revise and review. Sonnet or Haiku writes it once (first fact-check) and reads it in the second.

**Gaps between stages stay under 5 minutes.** We measured 171 s from the start of the draft to the start of the first review, 84 s from review to revise, and 234 s from revise to the second review. The 5-minute TTL therefore holds without the 1-hour premium, and a miss costs one 1.25× re-write. The single-use tail (notebook and reports) is then written at 1.25× instead of 2×.

**Arithmetic for run 1:**

| Variant | Total |
| --- | --- |
| Baseline | $2.95 |
| Shared prefix, 1-hour TTL | $2.30 |
| Shared prefix, 5-minute TTL | **$1.93** |

The same rule makes ADR-0005's 23-lesson budget much safer. Under the spike's own scaling assumption (only the source grows with chapter size, ×1.56), the course drops from the spike's $74–83 projection to about $47: $1.93 plus about $0.11 of extra source per lesson, × 23.

### 3. Batch and asynchronous APIs

| Source | Date | Key facts |
| --- | --- | --- |
| Anthropic, Batch processing, https://platform.claude.com/docs/en/build-with-claude/batch-processing | read 2026-11-22 | 50 % off input and output. Most batches finish within 1 hour; results come at the latest after 24 h; at most 100,000 requests or 256 MB per batch. It stacks with caching, but cache hits in a batch are best-effort, "30 % to 98 %". The docs advise the 1-hour TTL for shared context in batches |
| Anthropic, Pricing (Managed Agents section), https://platform.claude.com/docs/en/about-claude/pricing | read 2026-11-22 | "Sessions are stateful and interactive. There is no batch mode" (Managed Agents). Fast mode is not available with Batch |
| Claude Code docs: prompt caching and cost tracking (above) | read 2026-11-22 | Neither the Agent SDK nor the CLI documents a batch mode. Both send ordinary Messages requests |

**For us:**
- **Batch is out of reach through the Agent SDK.** It is a raw Messages API feature that needs an API key. ADR-0005 and ADR-0023 bill compile work as Agent SDK credit on the owner's subscription.
- **The theoretical gain is −50 % on everything.** Our stages are sequential, so batching would go across lessons (stage k of all 23 lessons in one batch), with hours of latency per stage.
- **Switching would contradict accepted ADRs.** It would need a new ADR with `status: proposed` and the owner's decision. Not in the experiment plan.

### 4. Model cascades and cheap fact-checking

| Source | Date | Key numbers |
| --- | --- | --- |
| Chen, Zaharia, Zou, "FrugalGPT", https://arxiv.org/abs/2305.05176 | 2023-05-09 | Cascades match GPT-4 at up to 98 % lower cost, or +4 % accuracy at the same cost |
| Ong et al., "RouteLLM: Learning to Route LLMs with Preference Data", https://arxiv.org/abs/2406.18665 | 2024-06-26, rev. 2025-02-23 | Routers cut cost by over 2× in some cases with no loss in quality |
| Tang, Laban, Durrett, "MiniCheck: Efficient Fact-Checking of LLMs on Grounding Documents", https://arxiv.org/abs/2404.10774 (EMNLP 2024) | 2024-04-16 | A 770M Flan-T5 checker reaches GPT-4 accuracy on LLM-AggreFact at 400× lower cost |
| LLM-AggreFact leaderboard, https://llm-aggrefact.github.io/ | read 2026-11-22 (no date shown) | Bespoke-MiniCheck-7B 77.4, Claude-3.5 Sonnet 77.2, GPT-4o 75.9, MiniCheck-Flan-T5-L (0.8B) 75.0; top entry 79.5 (27B) |
| Bespoke-MiniCheck-7B model card, https://huggingface.co/bespokelabs/Bespoke-MiniCheck-7B | read 2026-11-22 | Language metadata `en` only. CC BY-NC 4.0; commercial use needs a licence |
| Wei et al., "Long-form factuality in large language models" (SAFE), https://arxiv.org/abs/2403.18802 | 2024-03-27 | Claim decomposition plus per-claim verification: 72 % agreement with humans on about 16k facts; wins 76 % of disagreements; more than 20× cheaper than human annotators |
| HalluGuard, https://arxiv.org/abs/2510.00880 | 2025-10 | A 4B model reaches 75.7 % balanced accuracy on LLM-AggreFact (paper-reported) |
| ClearFacts study, https://arxiv.org/html/2506.13342v2 | 2025-06 | Small fine-tuned checkers lag larger models on multi-hop claims |
| Anthropic, Claude Haiku 5.5 overview, https://platform.claude.com/docs/en/models/haiku-5-5/overview | released 2026-10-07; read 2026-11-22 | $0.10/$0.50 per MTok for prompts ≤ 100k tokens, and $0.50/$2.50 above. 1M context, adaptive thinking, default effort `medium`; positioned for "classification, extraction, and routing" |

**For us:**
- **The cheapest credible fact-checker is Claude Haiku 5.5, not a local NLI model.** MiniCheck-class models are English-only. Our course is Russian with formulas, and the licence of the 7B model is non-commercial.
- **The shape matches SAFE and FActScore:** the draft already emits claims with page citations, so verification per claim is natural.
- **Cascade:** Haiku checks every claim. Only claims it marks `unsupported`, `contradicted` or `miscited`, plus a random 10 % audit sample, go to Sonnet. The audit sample bounds the miss rate that a rubber-stamping Haiku could hide.
- **Haiku's price jumps 5× above 100k prompt tokens.** L02's fact-check prompt is about 51k tokens. L01, at 135,935 characters (about 54k source tokens plus a larger notebook), may approach 100k. Split the check by section if it does.

### 5. Output-token reduction

| Source | Date | Key facts |
| --- | --- | --- |
| Anthropic, Effort, https://platform.claude.com/docs/en/build-with-claude/effort | read 2026-11-22 | Effort affects every output token, including thinking. Opus 5.5 defaults to `medium`, and its adaptive thinking is always on and cannot be disabled. Sonnet 5.5 defaults to `high`. "Run an effort sweep on your own evals." Effort is a behavioural signal, not a strict budget; `max_tokens` is the hard cap |
| Anthropic, Citations, https://platform.claude.com/docs/en/build-with-claude/citations | read 2026-11-22 | "`cited_text` does not count toward output tokens"; citations are guaranteed to point into the provided documents. Citations cannot be combined with structured outputs (`output_config.format`). They need `document` blocks on the Messages API |
| "To Diff or Not to Diff? Structure-Aware and Adaptive Output Formats for Efficient LLM-based Code Editing" (AdaEdit), https://arxiv.org/abs/2604.27296 (ACL Findings 2026) | 2026-04 | Choosing diff or full output per edit keeps accuracy and cuts latency and cost by over 30 %. Diffs cost more than full output when changes are pervasive |
| Cascaded code editing, https://arxiv.org/pdf/2604.19201 | 2026-04 | Search/replace uses slightly fewer tokens than whole-file output; unified diff pays a "context tax" and uses more |
| Diff-XYZ, https://arxiv.org/html/2510.12487v2 | 2025-10 | Search-replace works best for larger models |

**Draft output is mostly real content, not thinking.** Draft output was 17.6k tokens, of which 16.2k were non-thinking. An effort sweep therefore mainly shortens the text, and the saving is uncertain (< 10 % of output, unmeasured).

**Revise is the large target.** Its 20–26k output tokens ($0.40–0.52) re-emit the whole notebook to change a minority of cells: 16 findings over 39 cells in run 1. A patch format fits this well:
- `{"op": "replace" | "insert" | "delete", "cell_index", "cell"}`, applied server-side and validated against the cell schema;
- per AdaEdit, fall back to the full notebook when more than about half of the cells change.

**The Citations API is not usable as-is.** It would cut output (quotes become free), but it is incompatible with our JSON structured output and needs raw `document` blocks. Whether the Agent SDK can pass them is **unverified**. Not recommended now.

### 6. Context compression

| Source | Date | Key numbers |
| --- | --- | --- |
| Jiang et al., "LongLLMLingua", https://arxiv.org/abs/2310.06839 (ACL 2024) | 2023-10-10, rev. 2024-08-12 | +21.4 % on NaturalQuestions with about 4× fewer tokens (GPT-3.5-Turbo); 94 % cost reduction on LooGLE; 1.4–2.6× lower latency at 2–6× compression |
| Pan et al., "LLMLingua-2", https://arxiv.org/abs/2403.12968 (Findings ACL 2024) | 2024-03-19 | 2–5× compression; 1.6–2.9× lower end-to-end latency; no reported evaluation on non-English text |
| Jha et al., "Characterizing Prompt Compression Methods for Long Context Inference", https://arxiv.org/abs/2407.08892 | 2024-07-11 | Extractive compression (rerank and keep sentences) often beats token pruning, with up to 10× compression and minimal degradation; abstractive summaries help only on summarization |

**Not recommended for compile.** After lever 1, the source is a cached prefix read at 0.05×, so compressing it saves cents per course.
- **Token pruning is unsafe here.** It drops function words and symbols, so it can damage Russian morphology, formulas and our `=== PDF page N ===` markers, which citations depend on.
- **The extractive form is a variant of RAG** (topic 1) and can be tested there.
- **The tutor is where compression could pay** (lever 4): its context is not shared across many calls. Defer to S6.SRV-TUTOR.

### 7. Embedding and ingest cost

| Source | Date | Key numbers |
| --- | --- | --- |
| Our spike `spikes/embeddings-local/RESULT.md` | 2026-11-20 | multilingual-e5-small 14.35 chunks/s, full pilot in 2.7 min; bge-m3 1.18 chunks/s, 32.3 min (4-CPU Docker VM). Equal recall@10 on the draft question set |
| Our spike `spikes/embed-service/RESULT.md` (branch `origin/0/spike.embed-service`) | 2026-11-22 | bge-m3 at 8 CPUs: 1.05–1.72 prose chunks/s. Worst admitted batch: 3.6 GiB RSS. bf16 on CPU is 9–30× slower: rejected |
| Qwen3-Embedding model card, https://huggingface.co/Qwen/Qwen3-Embedding-0.6B; paper https://arxiv.org/abs/2506.05176 | 2025-06-05 | 0.6B parameters, 32k context, Matryoshka dimensions 32–1024, 100+ languages. MMTEB multilingual mean 64.33, against bge-m3 59.56 and multilingual-e5-large-instruct 63.22 (vendor table, not Russian-specific) |
| Hugging Face, "Binary and Scalar Embedding Quantization", https://huggingface.co/blog/embedding-quantization | 2024-03-22 | int8: 4× smaller, about 97 % retrieval quality retained (about 99 % with a ×4 rescore), 3.66× faster search. Binary: 32× smaller, 92.5 % retained (96.45 % with rescore), about 25× faster |
| Faysse et al., "ColPali", https://arxiv.org/abs/2407.01449 (ICLR 2025) | 2024-06-27, rev. 2025-02-28 | ViDoRe nDCG@5 81.3, against 67.0 for the best text pipeline (with BGE-M3). Indexing 0.39 s/page vs 7.22 s/page for a parse + OCR pipeline. **257.5 KB per page** vs 8.6 KB for BGE-M3 (about 30×) |

**For us:**
- **Embedding costs $0 of API per page,** so it is not a $/page lever.
- **Incremental re-embedding by content hash (lever 5)** removes repeat CPU work at zero risk.
- **int8 storage** is a free 4× memory cut if the index outgrows RAM. It does not at 2,285 chunks, so it is not needed now.
- **Recall is the quality bottleneck behind RAG,** not cost: recall@10 is 0.675 dense. Qwen3-Embedding-0.6B is the candidate to benchmark on questions-v2 against bge-m3.
- **ColPali-style page images** would help formula pages but need GPU-class compute and 30× the storage. Not for v1 on a Mac CPU.

### 8. Evaluating that quality did not regress

| Source | Date | Key numbers |
| --- | --- | --- |
| Gao et al., "Enabling Large Language Models to Generate Text with Citations" (ALCE), https://arxiv.org/abs/2305.14627 (EMNLP 2023) | 2023-05-24 | Citation recall: is the statement supported by its citations? Citation precision: is each citation needed? The best models lack full citation support 50 % of the time on ELI5 |
| Zheng et al., "Judging LLM-as-a-Judge with MT-Bench and Chatbot Arena", https://arxiv.org/abs/2306.05685 | 2023-06-09 | Strong judges agree with humans more than 80 % of the time. Known biases: position, verbosity, self-enhancement, limited reasoning |
| Thakur et al., "Judging the Judges", https://arxiv.org/abs/2406.12624 (GEM² 2025) | 2024-06-18, rev. 2025-08-18 | Only the largest judges align reasonably with humans. Percent agreement hides score differences of up to 5 points, so use chance-corrected measures. Judges tend to be lenient |
| Huang et al., "Large Language Models Cannot Self-Correct Reasoning Yet", https://arxiv.org/abs/2310.01798 (ICLR 2024) | 2023-10-03 | Self-correction without external feedback does not improve results and sometimes degrades them. This matches our non-converging loop and supports lever 2 |

**Rules for our A/B:**
- **Use a fixed seeded-error fixture.** Inject known faults into a frozen notebook: wrong page, changed number, negated claim, claim absent from the source. Fact-check recall on known errors is then a ground truth, not a judge's opinion.
- **Report Cohen's κ, not only percent agreement,** for claim-level verdicts against the baseline.
- **Never let a model judge its own outputs.** For pairwise comparisons, randomize order and judge both orders.
- **Use the owner's G2 as the human anchor:** a spot-check of 20 random claims per variant.
- **Compute citation precision and recall the ALCE way,** with the fact-checker of the baseline configuration as the support judge.

## Recommended experiment plan for V.cost-optimize

**Budget:** $30 test budget minus $5.46 spent leaves **$24.54**. The plan below spends about **$14.5** and keeps about $10 in reserve for re-runs (variance between runs was −18 % in the spike).

**Frozen inputs:**
- the L02 source (Docling, configuration D);
- the four notebooks the spike produced (draft and revised, runs 1 and 2, in gitignored `data/spikes/compile-cost/`);
- a seeded-error fixture built from them by a committed generator, holding claim ids and fault types only and no book text in git.

**Pass rule for every variant:**
- G2 does not regress;
- fact-check verdict agreement with the baseline is κ ≥ 0.7, and seeded-error recall is ≥ the baseline's;
- no rise in blocking findings that the owner confirms;
- tokens and $ per stage are recorded before and after, in the DoD report's cost columns.

| Exp | Variant (A vs B) | Runs | Metrics | Pass threshold | Expected spend |
| --- | --- | --- | --- | --- | --- |
| E0 | Baseline: the current SRV-PIPELINE prompts with an itemized fact-check, full compile | 1 | $ and tokens per stage, claims, verdicts, blocking findings | Reference | $3.0 |
| E1 | Lever 1: shared system-prompt prefix, `CLAUDE_CODE_PROMPT_CACHE_TTL=5m`, versus E0 | 1 full compile | `cache_read_input_tokens` on stages 2–6 ≥ 30k; $ per stage; outputs within E0's variance (claims ±25 %, verdict κ ≥ 0.7) | ≥ 25 % cheaper than E0, no G2 regression | $2.0 |
| E2 | Lever 3: fact-check Sonnet 5.5 vs Haiku 5.5 (effort `medium` and `high`) on 4 frozen notebooks + the seeded fixture, 2 repeats | 4 × 2 × 3 | Seeded-error recall and precision, κ against Sonnet, itemized output present | Haiku recall ≥ 0.9 and ≥ Sonnet's; κ ≥ 0.7; summary-only output = fail | $2.4 |
| E3 | Lever 4: patch-based revise vs full revise, from the frozen draft + review | 2 + 2 | Revise output tokens, patch apply rate, fact-check (cheapest passing checker) on the result | Apply rate 100 %; output −30 % or more; fact-check problems ≤ the full revise | $2.8 |
| E4 | Lever 7 (quality): claim-local fact-check (cited page ±1 per claim) vs full chapter, on the E2 winner | 4 | Seeded-error recall, miscited detection, tokens | Recall ≥ the full-chapter variant; adopt only if better (LaRA's hallucination-detection result) | $0.5 |
| E5 | Lever 5: review on Opus `medium` vs Opus `low` vs Sonnet 5.5 `high`, on 2 frozen notebooks | 2 × 3 | Recall of E0-Opus blocking findings, new valid findings (owner spot-check) | Recall of blocking findings ≥ 0.9 | $1.5 |
| E6 | Stack of the winners, full compile, vs E0 | 2 | $ per page, all gates | ≤ $0.06 per page and every pass rule | $2.6 |
| — | Lever 2 (loop gating) | — | Decided by owner ask (a); measured as a by-product of E0, E1 and E6 (would the loop have been skipped?) | — | $0 |

**Order:** E1 first. It changes the cost basis of every later experiment, so E2–E5 must run on the E1 layout to count correctly. Then E2, E3, E4 and E5 (all on frozen inputs, cheap), then E6. Record each result in `spikes/cost-optimize/RESULT.md` and in `docs/progress.yaml` (`live_runs_by_agent`).

## Caveats

- **Prices:**
  - Every dollar figure uses the list prices read on 2026-11-22. They match `spec/fixtures/SRV-AGENT/pricing.yaml` (as of 2026-11-03) for Opus 5.5 and Sonnet 5.5.
  - Haiku 5.5 is not in that fixture yet. It would need adding before E2.
  - The spike's own RESULT.md inferred a 0.1× read rate; the provider's 0.05× for the 5.5 models is used here. Reads are a negligible share either way.
- **Agent SDK behaviour is documented, not measured.** Three things in lever 1 are unverified on our pinned `claude-agent-sdk` 0.2.186 and must be confirmed by E1's `cache_read_input_tokens`:
  - a custom `system_prompt` string with the source forms its own cached layer;
  - `CLAUDE_CODE_PROMPT_CACHE_TTL` is honoured;
  - Python's `{"type": "file"}` system prompt handles a 60k-character source.
- **The 5-minute TTL assumes stages run back to back.** A queue delay of more than 5 minutes between same-model stages costs one re-write at 1.25×. The worst case is still cheaper than today.
- **Lever 2 has no external effect size.** Its $ figure depends on how often lessons would skip the loop, which is unknown until the owner settles ask (a).
- **Lever 3's quality is the open risk.** No public benchmark covers Haiku 5.5 on Russian document-grounded fact-checking. LLM-AggreFact is English. The seeded fixture is our only ground truth.
- **Lever 4's savings come from code editing,** not notebook JSON. Treat 30–65 % as a range to measure.
- **Not verified from the primary source:** OP-RAG's F1 numbers; the NeurIPS 2026 prefix-stability paper beyond its listing; HalluGuard's score.
- **Per-page figures scale with chapter size.** L02 is the optimistic sample: 25 pages and 60,034 characters, against 33 pages and 135,935 characters for L01. Absolute savings per lesson grow with the source. The share saved by lever 1 also grows, because the source is the part that becomes a cache read.
