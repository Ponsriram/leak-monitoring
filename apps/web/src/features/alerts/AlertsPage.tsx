import { useState } from "react";
import { EmptyState, ErrorState, TableSkeleton } from "../../components/states";
import { formatDate, formatNumber } from "../../lib/format";
import {
  useAlertEvents,
  useAlerts,
  useCreateAlert,
  useDeleteAlert,
  useToggleAlert,
  type Alert,
} from "../../lib/queries";

const MATCH_KINDS: { value: Alert["matchKind"]; label: string; help: string }[] = [
  { value: "substring", label: "Contains", help: "Victim name or domain contains this text" },
  { value: "exact", label: "Exact match", help: "Victim name is exactly this" },
  { value: "domain", label: "Domain", help: "Victim domain matches, including subdomains" },
  { value: "actor_group", label: "Ransomware group", help: "Any leak from this group" },
];

export function AlertsPage() {
  const alerts = useAlerts();
  const events = useAlertEvents();
  const createAlert = useCreateAlert();
  const toggleAlert = useToggleAlert();
  const deleteAlert = useDeleteAlert();

  const [name, setName] = useState("");
  const [matchKind, setMatchKind] = useState<Alert["matchKind"]>("substring");
  const [matchValue, setMatchValue] = useState("");
  const [target, setTarget] = useState("");
  const [formError, setFormError] = useState<string | null>(null);

  async function handleCreate(event: React.FormEvent) {
    event.preventDefault();
    setFormError(null);
    try {
      await createAlert.mutateAsync({
        name,
        matchKind,
        matchValue,
        channel: "email",
        target,
      });
      setName("");
      setMatchValue("");
      setTarget("");
    } catch (error) {
      setFormError(error instanceof Error ? error.message : "Could not create the alert.");
    }
  }

  const selectedKind = MATCH_KINDS.find((kind) => kind.value === matchKind);

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>Alerts</h1>
          <p className="page-sub">
            Get notified when a new leak matches one of your rules.
          </p>
        </div>
      </div>

      <section className="card" style={{ marginBottom: 20 }}>
        <div className="card-head">
          <h2>New alert</h2>
        </div>
        <div className="card-body">
          {formError && (
            <div className="form-error" role="alert">
              {formError}
            </div>
          )}

          <form onSubmit={handleCreate}>
            <div
              style={{
                display: "grid",
                gridTemplateColumns: "repeat(auto-fit, minmax(190px, 1fr))",
                gap: 12,
              }}
            >
              <div className="field">
                <label htmlFor="alert-name">Name</label>
                <input
                  id="alert-name"
                  type="text"
                  required
                  value={name}
                  placeholder="Acme Corp watch"
                  onChange={(e) => setName(e.target.value)}
                />
              </div>

              <div className="field">
                <label htmlFor="alert-kind">Match type</label>
                {/*
                  A fixed list, not a free-text pattern. The old form accepted any string and
                  the server dropped it straight into a Mongo $regex — so "(a+)+$" was a
                  denial of service. None of these options can express a pathological pattern.
                */}
                <select
                  id="alert-kind"
                  value={matchKind}
                  onChange={(e) => setMatchKind(e.target.value as Alert["matchKind"])}
                >
                  {MATCH_KINDS.map((kind) => (
                    <option key={kind.value} value={kind.value}>
                      {kind.label}
                    </option>
                  ))}
                </select>
                <span style={{ color: "var(--muted)", fontSize: 12 }}>
                  {selectedKind?.help}
                </span>
              </div>

              <div className="field">
                <label htmlFor="alert-value">Match value</label>
                <input
                  id="alert-value"
                  type="text"
                  required
                  minLength={2}
                  value={matchValue}
                  placeholder="acme"
                  onChange={(e) => setMatchValue(e.target.value)}
                />
              </div>

              <div className="field">
                <label htmlFor="alert-target">Send to</label>
                <input
                  id="alert-target"
                  type="email"
                  required
                  value={target}
                  placeholder="you@example.com"
                  onChange={(e) => setTarget(e.target.value)}
                />
              </div>
            </div>

            <button
              type="submit"
              className="btn btn-primary"
              disabled={createAlert.isPending}
            >
              {createAlert.isPending ? "Creating…" : "Create alert"}
            </button>
          </form>
        </div>
      </section>

      <section className="card" style={{ marginBottom: 20 }}>
        <div className="card-head">
          <h2>Your alerts</h2>
        </div>

        {alerts.isPending ? (
          <TableSkeleton rows={3} cols={5} />
        ) : alerts.isError ? (
          <ErrorState error={alerts.error} onRetry={alerts.refetch} />
        ) : alerts.data.data.length === 0 ? (
          <EmptyState title="No alerts yet">
            <p>Create one above to be notified when a matching leak appears.</p>
          </EmptyState>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Name</th>
                  <th>Rule</th>
                  <th>Send to</th>
                  <th>Fired</th>
                  <th>Created</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {alerts.data.data.map((alert) => (
                  <tr key={alert.id}>
                    <td className="strong">
                      {alert.name}
                      {!alert.enabled && (
                        <span className="chip neutral" style={{ marginLeft: 8 }}>
                          paused
                        </span>
                      )}
                    </td>
                    <td>
                      {MATCH_KINDS.find((k) => k.value === alert.matchKind)?.label}{" "}
                      <code>{alert.matchValue}</code>
                    </td>
                    <td className="mono">{alert.target}</td>
                    <td className="num">{formatNumber(alert.triggerCount)}</td>
                    <td className="num">{formatDate(alert.createdAt)}</td>
                    <td style={{ whiteSpace: "nowrap", textAlign: "right" }}>
                      <button
                        type="button"
                        className="btn btn-sm"
                        onClick={() =>
                          toggleAlert.mutate({ id: alert.id, enabled: !alert.enabled })
                        }
                      >
                        {alert.enabled ? "Pause" : "Resume"}
                      </button>{" "}
                      <button
                        type="button"
                        className="btn btn-sm btn-danger"
                        onClick={() => deleteAlert.mutate(alert.id)}
                      >
                        Delete
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section className="card">
        <div className="card-head">
          <h2>Delivery history</h2>
        </div>

        {events.isPending ? (
          <TableSkeleton rows={3} cols={4} />
        ) : events.isError ? (
          <ErrorState error={events.error} onRetry={events.refetch} />
        ) : events.data.data.length === 0 ? (
          <EmptyState title="Nothing delivered yet">
            <p>
              This fills in once the collection pipeline is running and a new leak matches
              one of your rules.
            </p>
          </EmptyState>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Alert</th>
                  <th>Victim</th>
                  <th>Matched on</th>
                  <th>Status</th>
                  <th>Sent</th>
                </tr>
              </thead>
              <tbody>
                {events.data.data.map((event) => (
                  <tr key={event.id}>
                    <td className="strong">{event.alertName}</td>
                    <td>{event.victimName ?? "—"}</td>
                    <td className="mono">{event.matchedOn}</td>
                    <td>{event.status}</td>
                    <td className="num">{formatDate(event.sentAt ?? event.createdAt)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}
