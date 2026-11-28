"""
Gunicorn config for Sarhad's multi-worker production deployment.

Why this file exists (production-readiness-checklist.md, item 1 —
"multi-worker architecture for batch throughput"):

- `preload_app = True`: app.py — including the expensive Layer-1 model
  load (`default_model_backend()`, called at module import time) — is
  imported exactly ONCE, in the master process, before any worker is
  forked. Every forked worker then shares those already-loaded model
  weights via Linux's copy-on-write fork semantics instead of loading
  its own separate copy. This is the detail that keeps RAM roughly flat
  as workers scale, rather than multiplying per worker.

- `workers`: one worker process per vCPU core (WEB_CONCURRENCY env var,
  defaulting to the machine's own CPU count if unset). Gunicorn's own
  preforked accept-queue is the "dispatcher" — the OS hands each new
  connection to whichever worker is free to accept() it, so no separate
  queue/dispatcher code is needed on top of this.

- `post_fork` hook: PyTorch lazily creates its own intra-op thread pool
  (via OpenMP/MKL) on first use. A thread pool created in the parent
  before a fork does not survive `os.fork()` cleanly in the child — a
  well-known fork+threading hazard that can hang or silently serialize
  a worker's very first inference call. Explicitly capping each
  worker's own torch thread count right after the fork sidesteps this,
  and also avoids N worker processes each trying to claim every CPU
  core for their own intra-op math (oversubscription): with one worker
  per vCPU, each worker should mostly use ~1 thread for its own tensor
  ops, not all of them.

- Single-process dev path is untouched: this file is only used when
  app.py re-execs into gunicorn (see its `__main__` block). Set
  SARHAD_SINGLE_PROCESS=1 to skip gunicorn entirely for local testing.
"""

import os

bind = f"0.0.0.0:{os.environ.get('PORT', '8000')}"
workers = int(os.environ.get("WEB_CONCURRENCY") or (os.cpu_count() or 2))
worker_class = "uvicorn_worker.UvicornWorker"
preload_app = True

# Model load + first inference can be slow on a cold CPU instance;
# give workers room rather than have gunicorn kill a slow-but-healthy
# worker mid-request.
timeout = int(os.environ.get("GUNICORN_TIMEOUT", "120"))
graceful_timeout = 30

accesslog = "-"
errorlog = "-"


def post_fork(server, worker):
    """Cap each worker's own PyTorch intra-op thread count. See module
    docstring above for why this matters with preload_app=True."""
    try:
        import torch

        torch.set_num_threads(1)
        server.log.info("worker %s: torch.set_num_threads(1)", worker.pid)
    except Exception as exc:  # noqa: BLE001 - never fail worker startup over this
        server.log.warning("worker %s: could not cap torch threads (%r)", worker.pid, exc)
