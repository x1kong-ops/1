# Repository instructions for research assistants

Treat this repository as an external, versioned memory source for the U.S. Stock project.

Before answering a question that depends on Guolaoxing articles, historical forecasts, people, categories, charts, or prior project conclusions:

1. Read `memory/IMMEDIATE_MEMORY_STATUS.json` first. If `PROJECT_MEMORY_STATUS.json` exists, read it too and prefer the fresher successful status.
2. Search the directly readable curated claims first: `memory/curated_market_claims.*.jsonl`.
3. Search the directly readable curated article summaries next: `memory/curated_articles.*.jsonl`.
4. After automatic materialization, use `figures.jsonl` for chart context and review status.
5. Search `memory/site_index_recent.jsonl` and `memory/site_index_shards/*.jsonl` only for title/person/date/category discovery and source location.
6. Keep `phase3_profiles.jsonl` and `phase3_queue.jsonl` isolated unless facts have been independently verified and approved for the core bridge.
7. Label astrology/命理 content as non-scientific source context; never use it as a standalone trading signal.
8. Verify all current prices, filings, earnings, company guidance, macro data, laws, and other time-sensitive facts from fresh primary sources.
9. Distinguish clearly among source opinion, curated summary, current verified fact, and new inference.
10. Cite or identify the exact knowledge-base record and its source URL when it materially affects an answer.

Do not claim that this repository is model-parameter memory. Retrieval from the connected repository is required when its contents are relevant.
