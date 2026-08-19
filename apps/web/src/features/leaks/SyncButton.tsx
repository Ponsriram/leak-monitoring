import { useEffect, useRef } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { formatRelative } from "../../lib/format";
import { useCrawlStatus, useRequestCrawl, type CrawlRequest } from "../../lib/queries";

/**
 * "Sync now" — ask the collection worker for a crawl, then say what happened.
 *
 * Two things this is careful about, because a refresh button that lies is worse than none.
 *
 * It does not claim to have collected anything. Clicking queues a row the Python worker
 * picks up within seconds; if a crawl is already running the request waits behind it, and
 * the button says "queued" rather than pretending to work. The states below are the
 * request's real lifecycle, read back from the database.
 *
 * And it refreshes the table when the crawl *finishes*, not when the click succeeds. The
 * obvious implementation — invalidate the leak queries in `onSuccess` — refetches the
 * instant the request row is written, minutes before a single page has been fetched, so the
 * table redraws with exactly the rows it already had and the sync looks like it did
 * nothing.
 */

type Props = {
  /** Omit to crawl every enabled source. */
  sourceSlug?: string;
};

function isSettled(request: CrawlRequest | null | undefined): boolean {
  return (
    request != null &&
    request.status !== "queued" &&
    request.status !== "running"
  );
}

export function SyncButton({ sourceSlug }: Props) {
  const client = useQueryClient();
  const status = useCrawlStatus();
  const request = useRequestCrawl();

  const latest = status.data?.latest ?? null;
  const busy = Boolean(
    status.data?.running || latest?.status === "queued" || latest?.status === "running",
  );

  /**
   * Refetch everything derived from leaks once a crawl settles.
   *
   * Keyed on the request id as well as its status: two syncs in a row both end at
   * "succeeded", and watching the status alone would miss the second one entirely.
   */
  const lastSettled = useRef<number | null>(null);
  useEffect(() => {
    if (!isSettled(latest)) return;
    if (lastSettled.current === latest!.id) return;

    // The first render after a page load also lands here, with whatever crawl last
    // finished. Recording it without invalidating avoids a redundant refetch of data the
    // page has only just loaded.
    const isFirstObservation = lastSettled.current === null;
    lastSettled.current = latest!.id;
    if (isFirstObservation) return;

    void client.invalidateQueries({ queryKey: ["leaks"] });
    void client.invalidateQueries({ queryKey: ["stats"] });
    void client.invalidateQueries({ queryKey: ["sources"] });
  }, [latest, client]);

  const label = busy
    ? latest?.status === "queued"
      ? "Queued…"
      : "Syncing…"
    : "Sync now";

  return (
    <div className="sync">
      <button
        type="button"
        className="btn btn-primary"
        onClick={() => request.mutate(sourceSlug)}
        disabled={busy || request.isPending}
        // The disabled state alone does not explain itself, and "why is the button greyed
        // out" is the first thing anyone asks of a sync control.
        title={
          busy
            ? "A crawl is already in flight. This one will run when it finishes."
            : "Fetch every enabled source now"
        }
      >
        {busy && <span className="spinner" aria-hidden="true" />}
        {label}
      </button>

      <span className="sync-note" role="status" aria-live="polite">
        <SyncNote
          busy={busy}
          latest={latest}
          queued={status.data?.queued ?? 0}
          error={request.error}
        />
      </span>
    </div>
  );
}

function SyncNote({
  busy,
  latest,
  queued,
  error,
}: {
  busy: boolean;
  latest: CrawlRequest | null;
  queued: number;
  error: unknown;
}) {
  if (error) {
    return <span className="sync-fail">{(error as Error).message}</span>;
  }

  if (busy) {
    if (latest?.status === "queued") {
      return <>Waiting for the collection worker{queued > 1 ? ` (${queued} queued)` : ""}…</>;
    }
    return <>Fetching sources over Tor. This takes minutes, not seconds.</>;
  }

  if (!latest) return <>Never synced from here.</>;

  if (latest.status === "failed") {
    return (
      <span className="sync-fail">
        Last sync failed {formatRelative(latest.finishedAt)}
        {latest.error ? `: ${latest.error}` : ""}
      </span>
    );
  }

  return (
    <>
      Last sync {formatRelative(latest.finishedAt ?? latest.requestedAt)} —{" "}
      {latest.newLeaks} new, {latest.updatedLeaks} seen again
      {latest.failedSources > 0 && (
        <span className="sync-fail"> · {latest.failedSources} source(s) failed</span>
      )}
    </>
  );
}
