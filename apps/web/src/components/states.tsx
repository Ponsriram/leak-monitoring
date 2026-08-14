import type { ReactNode } from "react";
import { ApiError } from "../lib/api";

/**
 * Loading / empty / error states.
 *
 * The old app had none of these: every fetch failure went to `console.error` and the user
 * saw a permanently blank table with no indication anything had gone wrong.
 */

export function Loading({ label = "Loading…" }: { label?: string }) {
  return (
    <div className="state" role="status" aria-live="polite">
      {label}
    </div>
  );
}

export function TableSkeleton({ rows = 6, cols = 6 }: { rows?: number; cols?: number }) {
  return (
    <div className="card-body" role="status" aria-live="polite">
      <span className="visually-hidden">Loading results…</span>
      <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
        {Array.from({ length: rows }).map((_, r) => (
          <div key={r} style={{ display: "flex", gap: 8 }}>
            {Array.from({ length: cols }).map((__, c) => (
              <div
                key={c}
                className="skeleton"
                style={{ height: 20, flex: c === 0 ? 2 : 1 }}
              />
            ))}
          </div>
        ))}
      </div>
    </div>
  );
}

export function EmptyState({
  title,
  children,
}: {
  title: string;
  children?: ReactNode;
}) {
  return (
    <div className="state">
      <div className="state-title">{title}</div>
      {children}
    </div>
  );
}

export function ErrorState({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  const isApi = error instanceof ApiError;
  const message = error instanceof Error ? error.message : "Something went wrong.";

  return (
    <div className="state error" role="alert">
      <div className="state-title">Couldn’t load this</div>
      <div>{message}</div>
      {/* The request id ties a user-reported failure to a specific server log line. */}
      {isApi && error.requestId && (
        <div className="state-detail">request {error.requestId}</div>
      )}
      {onRetry && (
        <div style={{ marginTop: 14 }}>
          <button type="button" className="btn btn-sm" onClick={onRetry}>
            Try again
          </button>
        </div>
      )}
    </div>
  );
}
