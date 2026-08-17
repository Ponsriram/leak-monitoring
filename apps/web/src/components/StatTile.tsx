import { formatNumber } from "../lib/format";

export function StatTile({
  label,
  value,
  text,
  note,
  loading,
  tone,
}: {
  label: string;
  value?: number | null;
  /** Pre-formatted value, for tiles whose figure is not a count (a time, say). */
  text?: string;
  note?: string;
  loading?: boolean;
  /** Optional status colour, for a tile that can be in a bad state. */
  tone?: "good" | "warning" | "critical";
}) {
  return (
    <div className="tile">
      <div className="tile-label">{label}</div>
      {loading ? (
        <div className="skeleton" style={{ height: 30, marginTop: 7, width: "60%" }} />
      ) : (
        <div
          className="tile-value"
          style={tone ? { color: `var(--status-${tone})` } : undefined}
        >
          {text ?? formatNumber(value)}
        </div>
      )}
      {note && <div className="tile-note">{note}</div>}
    </div>
  );
}
