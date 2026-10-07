"""The URL-queue crawler.

A crawl cycle seeds URLs into the `crawl_urls` table; workers claim them one at a time and
fetch them concurrently. The URL — not the source — is the unit of work. See `queue.py` for
the state machine and `worker.py` for the claim loops.
"""
