# References

Sources consulted for the architecture decisions, retrieval design, safety model and
reliability patterns. Cited by short key in the ADRs and design note.

## Retrieval-augmented generation and retrieval

- **[Lewis2020]** Lewis, P. et al. "Retrieval-Augmented Generation for Knowledge-Intensive NLP Tasks." NeurIPS 2020. arXiv:2005.11401.
- **[Karpukhin2020]** Karpukhin, V. et al. "Dense Passage Retrieval for Open-Domain Question Answering." EMNLP 2020. arXiv:2004.04906.
- **[Robertson2009]** Robertson, S., Zaragoza, H. "The Probabilistic Relevance Framework: BM25 and Beyond." Foundations and Trends in Information Retrieval 3(4), 2009.
- **[Cormack2009]** Cormack, G., Clarke, C., Buettcher, S. "Reciprocal Rank Fusion outperforms Condorcet and individual rank learning methods." SIGIR 2009.
- **[Gao2023]** Gao, Y. et al. "Retrieval-Augmented Generation for Large Language Models: A Survey." arXiv:2312.10997.
- **[Liu2023]** Liu, N. et al. "Lost in the Middle: How Language Models Use Long Contexts." TACL 2024. arXiv:2307.03172.
- **[Chen2023]** Chen, J. et al. "Benchmarking Large Language Models in Retrieval-Augmented Generation." AAAI 2024. arXiv:2309.01431. (Noise robustness, negative rejection, counterfactual robustness.)
- **[Es2023]** Es, S. et al. "RAGAS: Automated Evaluation of Retrieval Augmented Generation." arXiv:2309.15217.
- **[Asai2023]** Asai, A. et al. "Self-RAG: Learning to Retrieve, Generate, and Critique through Self-Reflection." ICLR 2024. arXiv:2310.11511.

## Agents, tool use and bounded control flow

- **[Yao2022]** Yao, S. et al. "ReAct: Synergizing Reasoning and Acting in Language Models." ICLR 2023. arXiv:2210.03629.
- **[Schick2023]** Schick, T. et al. "Toolformer: Language Models Can Teach Themselves to Use Tools." NeurIPS 2023. arXiv:2302.04761.
- **[Anthropic2024]** Anthropic. "Building Effective Agents." December 2024. https://www.anthropic.com/research/building-effective-agents (workflows versus agents; prefer the simplest composable pattern).
- **[LangGraph]** LangChain Inc. LangGraph documentation: persistence, checkpointers, human-in-the-loop interrupts. https://langchain-ai.github.io/langgraph/
- **[AnthropicToolUse]** Anthropic. Claude API documentation: tool use and structured outputs. https://docs.anthropic.com/

## Prompt injection and agent security

- **[Greshake2023]** Greshake, K. et al. "Not what you've signed up for: Compromising Real-World LLM-Integrated Applications with Indirect Prompt Injection." AISec 2023. arXiv:2302.12173.
- **[Willison2023]** Willison, S. "Prompt injection: What's the worst that can happen?" and the dual-LLM pattern series, 2023. https://simonwillison.net/series/prompt-injection/
- **[Wallace2024]** Wallace, E. et al. "The Instruction Hierarchy: Training LLMs to Prioritize Privileged Instructions." arXiv:2404.13208.
- **[Hines2024]** Hines, K. et al. "Defending Against Indirect Prompt Injection Attacks With Spotlighting." arXiv:2403.14720. (Delimiting and marking untrusted data.)
- **[Debenedetti2025]** Debenedetti, E. et al. "Defeating Prompt Injections by Design (CaMeL)." arXiv:2503.18813. (Capability separation between planner and untrusted data.)
- **[BeurerKellner2025]** Beurer-Kellner, L. et al. "Design Patterns for Securing LLM Agents against Prompt Injections." arXiv:2506.08837. (Action-selector, plan-then-execute, dual LLM, code-then-execute patterns.)
- **[OWASP2025]** OWASP. "Top 10 for Large Language Model Applications, 2025." LLM01 Prompt Injection, LLM06 Excessive Agency, LLM02 Sensitive Information Disclosure. https://owasp.org/www-project-top-10-for-large-language-model-applications/
- **[NIST2023]** NIST. "AI Risk Management Framework (AI RMF 1.0)." January 2023.

## Reliability, idempotency and durable execution

- **[Helland2012]** Helland, P. "Idempotence Is Not a Medical Condition." ACM Queue 10(4), 2012.
- **[GarciaMolina1987]** Garcia-Molina, H., Salem, K. "Sagas." SIGMOD 1987. (Compensating actions for long-lived transactions.)
- **[Stripe]** Stripe. "Idempotent requests." API documentation. https://docs.stripe.com/api/idempotent_requests
- **[Temporal]** Temporal Technologies. "Durable execution" concepts documentation. https://docs.temporal.io/
- **[SQLiteWAL]** SQLite. "Write-Ahead Logging" and "Transaction" documentation. https://www.sqlite.org/wal.html
- **[SQLitePragma]** SQLite. "PRAGMA statements" (`user_version`, `table_info`) and "ALTER TABLE". https://www.sqlite.org/pragma.html (The version pragma is the conventional place to record a schema version, and both DDL and the pragma are transactional, which is what makes a migration and its stamp one atomic step.)
- **[Ambler2006]** Ambler, S., Sadalage, P. "Refactoring Databases: Evolutionary Database Design." Addison-Wesley, 2006. (Forward-only migration, and why a schema change and its version record belong in one transaction.)
- **[Nygard2018]** Nygard, M. "Release It! Design and Deploy Production-Ready Software." 2nd ed., Pragmatic Bookshelf, 2018. (Timeouts, circuit breakers, bulkheads.)
- **[Google2016]** Beyer, B. et al. "Site Reliability Engineering." O'Reilly, 2016. Chapter on handling overload and retries.

## Observability

- **[OTel]** OpenTelemetry. Semantic conventions for generative AI spans. https://opentelemetry.io/docs/specs/semconv/gen-ai/
- **[Sigelman2010]** Sigelman, B. et al. "Dapper, a Large-Scale Distributed Systems Tracing Infrastructure." Google Technical Report, 2010.

## Finance controls (domain)

- **[COSO2013]** COSO. "Internal Control, Integrated Framework." 2013. (Segregation of duties, control activities.)
- **[ISO27001]** ISO/IEC 27001:2022. Information security management. (Logging and access control clauses referenced by FIN-POL-010.)
- **[AusPayNet]** Australian Payments Network. Guidance on business email compromise and payment redirection fraud. https://www.auspaynet.com.au/
- **[ATO-GST]** Australian Taxation Office. "GST" and "Tax invoices": the 10% rate, the requirements for a valid tax invoice, and GST-free supplies. https://www.ato.gov.au/businesses-and-organisations/gst-excise-and-indirect-taxes/gst (Cited for the configured rate in `domain/rules/tax.py`. The policy corpus states a jurisdiction and no rate, so the rate is an environmental assumption and this is the authority behind the value chosen; the existence of GST-free supplies is why an understated tax is recorded rather than queried.)
- **Northstar Group policy corpus.** FIN-POL-001 to FIN-POL-012, `finance_rag_corpus/`. Synthetic, in-repo.

## Tooling and libraries

- pydantic v2 documentation. https://docs.pydantic.dev/
- FastAPI documentation. https://fastapi.tiangolo.com/
- `rank_bm25` (BM25Okapi implementation). https://github.com/dorianbrown/rank_bm25
- `sentence-transformers` and BAAI `bge-small-en-v1.5` model card. https://huggingface.co/BAAI/bge-small-en-v1.5
- `uv` package manager. https://docs.astral.sh/uv/
- Hypothesis property-based testing. https://hypothesis.readthedocs.io/
