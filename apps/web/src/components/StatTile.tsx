import { formatNumber } from "../lib/format";

export function StatTile({
  label,
  value,
  note,
  loading,
}: {
  label: string;
  value: number | null | undefined;
  note?: string;
  loading?: boolean;
}) {
  return (
    <div className="tile">
      <div className="tile-label">{label}</div>
      {loading ? (
        <div className="skeleton" style={{ height: 30, marginTop: 7, width: "60%" }} />
      ) : (
        <div className="tile-value">{formatNumber(value)}</div>
      )}
      {note && <div className="tile-note">{note}</div>}
    </div>
  );
}
