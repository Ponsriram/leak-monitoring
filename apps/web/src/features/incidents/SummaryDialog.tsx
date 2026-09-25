import { useEffect, useRef, useState } from "react";
import { Check, Close, Copy, FileText } from "../../components/icons";
import type { IncidentRow } from "../../lib/incidents";

/**
 * The summary cell and the dialog it opens.
 *
 * The cell shows the whole summary, never a clamped excerpt. The cell is also the button that
 * opens the dialog, which gives the text room to breathe and a one-click copy.
 *
 * Native <dialog> with `showModal()`: it brings the focus trap, Escape-to-close, the inert
 * page behind it and the backdrop, all of which a hand-rolled overlay has to reimplement and
 * usually gets one of wrong. Focus returns to the cell that opened it when it closes.
 */
export function SummaryCell({ row }: { row: IncidentRow }) {
  const [open, setOpen] = useState(false);
  const composed = row.summarySource === "composed";

  return (
    <>
      <button
        type="button"
        className="summary-btn"
        onClick={() => setOpen(true)}
        title="Open the summary to read or copy it"
      >
        <span className={`summary-text${composed ? " summary-composed" : ""}`}>
          {row.summary}
        </span>
      </button>
      {open && <SummaryDialog row={row} onClose={() => setOpen(false)} />}
    </>
  );
}

function SummaryDialog({ row, onClose }: { row: IncidentRow; onClose: () => void }) {
  const ref = useRef<HTMLDialogElement>(null);
  const [copied, setCopied] = useState<"idle" | "copied" | "failed">("idle");

  useEffect(() => {
    const dialog = ref.current;
    if (!dialog) return;
    dialog.showModal();
    return () => dialog.close();
  }, []);

  useEffect(() => {
    if (copied === "idle") return;
    const timer = window.setTimeout(() => setCopied("idle"), 1600);
    return () => window.clearTimeout(timer);
  }, [copied]);

  async function copy() {
    try {
      await navigator.clipboard.writeText(row.summary);
      setCopied("copied");
    } catch {
      // Clipboard access is permission-gated and missing over plain http in some browsers.
      setCopied("failed");
    }
  }

  const titleId = `summary-title-${row.id}`;
  const victim = row.victimName ?? row.victimDomain ?? "Unnamed victim";

  return (
    <dialog
      ref={ref}
      className="modal"
      aria-labelledby={titleId}
      onClose={onClose}
      // A click on the backdrop lands on the <dialog> itself; a click inside lands on a child.
      onClick={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div className="modal-head">
        <div className="modal-mark" aria-hidden="true">
          <FileText size={18} />
        </div>
        <div className="modal-title">
          <h2 id={titleId}>Incident summary</h2>
          <div className="modal-sub">
            Incident #{row.id} · {victim} · {row.summary.length.toLocaleString()} characters
          </div>
        </div>
        <button type="button" className="modal-close" onClick={onClose} aria-label="Close">
          <Close size={16} />
        </button>
      </div>

      <div className="modal-body">{row.summary}</div>

      {row.summarySource === "composed" && (
        <p className="modal-note">
          The leak site printed no description for this victim. This summary was composed from
          the listing's recorded fields.
        </p>
      )}

      <div className="modal-foot">
        <span className="sr-only" role="status" aria-live="polite">
          {copied === "copied" ? "Summary copied" : copied === "failed" ? "Copy failed" : ""}
        </span>
        <button type="button" className="btn btn-primary" onClick={copy}>
          {copied === "copied" ? <Check size={15} /> : <Copy size={15} />}
          {copied === "copied" ? "Copied" : copied === "failed" ? "Copy failed" : "Copy summary"}
        </button>
      </div>
    </dialog>
  );
}
