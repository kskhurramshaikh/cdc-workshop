# CDC Privacy Filter — Model Stack & Sizing Decision

**Status:** Decided, grounded in measurement. Supersedes the earlier "let's try ONNX" direction.

## 1. Stack decision: PyTorch fp32, unchanged

Four Layer-1 backends were benchmarked on identical input (an 830-token / 3000-char chunk, the production `MAX_CHUNK_CHARS`), on identical hardware, with correctness verified against the production pipeline's actual entity output:

| Backend | Warm latency | Verdict |
|---|---|---|
| **PyTorch fp32 (current, unchanged)** | ~1.2–1.3s | **Winner** |
| PyTorch bf16 | ~1.4s | No better — this CPU has no native bf16 matmul acceleration (no AMX), so bf16 just adds conversion overhead |
| ONNX Runtime int8 (raw `InferenceSession`) | ~2.5–3.5s | 2–3x **slower** |
| PyTorch dynamic int8 quantization | Did not complete in 170s | Abandoned — impractical for this architecture |

**Why ONNX int8 lost, not won:** `openai/privacy-filter` is a sparse MoE model (1.4B total params, ~50M active per token). Its speed comes from routing each token to a small subset of experts — lots of small, irregular gather/scatter operations. Generic ONNX Runtime's CPU execution provider doesn't fuse or vectorize that routing well; PyTorch's native implementation does. Quantization's usual CPU win only shows up when the dominant cost is dense matmul, which isn't the bottleneck here.

**Independent corroboration:** a third-party pure-Rust reimplementation of this exact model (`privacy-filter-rs`, using the Burn framework) found the same pattern from a completely different angle — their GPU backends (wgpu/Metal, f32 and f16) were *slower* than their CPU baseline for single-sequence inference, specifically citing "GPU transfer overhead on small batches" and "f16 precision kills MoE routing." Two independent implementations, two different languages/runtimes, same conclusion: **this specific architecture does not benefit from GPU or from generic quantization at single-document granularity.**

**Decision:** keep the existing PyTorch fp32 backend. No code change to Layer 1. The earlier caching, pre-warming, and batching work already shipped stands as the real performance lever.

## 2. Where GPU does make sense — and where it doesn't

This is the honest version of the "runs on CPU, scales to GPU" story:

- **Single-document, interactive redaction (what UC1/UC2 do today):** GPU is not the answer. MoE routing at batch-size-1 doesn't give a GPU enough parallel work to overcome transfer/kernel-launch overhead. A CPU core handles one document just fine — see sizing below.
- **Large concurrent batch jobs (e.g., a month-end reconciliation run across CDC's full custodial book — hundreds of statements/bills at once):** this is where GPU starts to earn its keep, because batching dozens-to-hundreds of sequences together gives the GPU enough simultaneous matmul work to amortize its overhead and actually exercise the MoE routing in parallel across the batch.
- We have not benchmarked this batched-GPU case directly (no GPU in this environment) — flagging that explicitly rather than inventing a number. Before quoting a hard "GPU kicks in at N concurrent documents" figure to CDC, that should be measured on an actual GPU instance (e.g., a Render GPU tier) with a realistic batch of real document sizes.

**Marketing-safe claim:** "The engine runs entirely on CPU for everyday interactive use — no GPU dependency, lower cost, simpler on-prem deployment. For large-scale batch processing (e.g. portfolio-wide month-end reconciliation), the same model can be moved to GPU infrastructure to increase throughput — that's a scaling option, not a requirement." This is defensible from measurement; a specific GPU throughput multiplier is not yet, and shouldn't be stated as one until measured.

## 3. Real sizing, grounded in actual CDC mock data

Measured against the real `ZTBL.pdf` statement (664,621 bytes, multi-account combined statement, 6 accounts' worth of transaction history): the production pipeline's redacted text output is approximately 14,000–16,000 characters. That gives a working ratio of:

**≈ 18,000 extracted text characters per 1 MB of real statement/bill PDF** (financial statement PDFs are size-heavy relative to their text content — repeated letterheads per page, layout/font tables, column positioning — so raw file size overstates actual processing load).

Measured Layer-1 throughput (single CPU core, current PyTorch fp32 backend): **~2,500 characters/second** warm.

### Single-document latency (the "max file size" question CDC will ask)

| CPU cores available to one request | Chars/sec | Time for a 1 MB PDF (~18,000 chars) | Time for the largest file in the current corpus (Bill 27, 840 KB) |
|---|---|---|---|
| 1 vCPU | ~2,500 | ~7.2s | ~6.0s |
| 2 vCPU (parallel chunking) | ~5,000 | ~3.6s | ~3.0s |
| 4 vCPU | ~10,000 | ~1.8s | ~1.5s |

Headline number for CDC: **on a single baseline vCPU, any document up to ~11 MB redacts within the 80-second interactive timeout already in place.** Nothing in the real corpus comes close — the largest file across both use cases is 840 KB. This is deliberately a generous, defensible ceiling, not a tight one: "default sizing handles any realistic single statement or bill with large headroom to spare; if a single document genuinely exceeds ~10 MB, that's when you'd add infra" is an honest, simple story for the workshop.

### The sizing lever that actually matters: concurrent throughput

Single-document latency isn't the real constraint — nothing in practice gets near it. The lever CDC will actually feel is **how many documents can be processed per minute during a batch run** (e.g., reconciling the full custodial book at month-end). That scales with vCPU count, roughly linearly, by running one inference worker process per core rather than relying on intra-request batching (our own test showed batching 8 chunks into one call on a 2-core box was *slower* than 8 sequential single-chunk calls — batching only helps once there are enough idle cores to actually exploit it).

| Instance tier (example) | vCPUs | Est. documents/minute (avg ~15KB extracted text each) | Good for |
|---|---|---|---|
| Starter (2 vCPU) | 2 | ~20 | Interactive UC1/UC2 calls, small batches |
| Standard (4 vCPU) | 4 | ~40 | Daily reconciliation batches |
| Pro (8 vCPU) | 8 | ~80 | Month-end, full-portfolio batch runs |
| GPU tier | — | *not yet measured* | Only worth adding once batch concurrency is high enough to keep a GPU saturated — to be validated empirically on Render before quoting |

**The sizing story for CDC, in one sentence:** *"Default sizing on a 2-vCPU instance handles any single document with huge headroom and comfortably supports interactive use; if you need to process your whole portfolio faster during month-end batch runs, you scale by adding CPU cores (linear, cheap, no architecture change) — and only if that batch gets large enough to need it, GPU is available as a further scaling step, not a baseline requirement."*

## 4. What's still open

- GPU batch-throughput numbers are not measured — call this out plainly if asked, rather than guessing.
- The chars/MB ratio (≈18,000/MB) is derived from one real document; it's representative of the corpus's dense tabular statements but should be treated as a working estimate, not a certified constant, until checked against a couple more samples (e.g. the Shajar Capital 840KB bill, BOK statements).
