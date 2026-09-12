import { ErrorState } from "../../components/states";
import { formatRelative } from "../../lib/format";
import type { HuntFinding, HuntFindingKind, HuntJob } from "../../lib/queries";

/**
 * The results of going out and looking.
 *
 * Findings are grouped by kind rather than listed chronologically, because they answer
 * different questions and arrive in an order decided by network latency — which is to say,
 * an order that means nothing to the reader. Registration, infrastructure and liveness are
 * facts about the domain; leak matches and corpus mentions are facts about our own data.
 *
 * The panel renders whatever exists at the moment it renders. A hunt in flight is not an
 * empty state: leak matches land in milliseconds and certificate transparency can take
 * twenty seconds, so there is almost always something to show while the rest is still out.
 */

const KIND_ORDER: HuntFindingKind[] = [
  "leak_match",
  "raw_page_mention",
  "registration",
  "infrastructure",
  "certificate",
  "liveness",
];

const KIND_LABEL: Record<HuntFindingKind, string> = {
  leak_match: "Listings",
  raw_page_mention: "Mentioned in crawled pages",
  registration: "Registration",
  infrastructure: "Infrastructure",
  certificate: "Certificate transparency",
  liveness: "Liveness & stack",
};

const KIND_HELP: Record<HuntFindingKind, string> = {
  leak_match: "Leak listings we had already collected for this company.",
  raw_page_mention:
    "Pages we crawled that name this company but which produced no leak record — extraction is lossy, so these are mentions that would otherwise be invisible.",
  registration: "Registrar and contact addresses, from the registry's RDAP service.",
  infrastructure: "Where the domain points, from DNS.",
  certificate:
    "Hostnames certified under this domain, from public certificate transparency logs.",
  liveness: "Whether the site answered, and what it appears to be built on.",
};

const STATUS_LABEL: Record<HuntJob["status"], string> = {
  queued: "Queued",
  running: "Looking…",
  succeeded: "Complete",
  partial: "Partly complete",
  failed: "Failed",
};

export function HuntPanel({
  job,
  findings,
  loading,
  error,
}: {
  job: HuntJob | null;
  findings: HuntFinding[];
  loading: boolean;
  error: unknown;
}) {
  if (error) {
    return (
      <section className="card">
        <div className="card-body">
          <ErrorState error={error} />
        </div>
      </section>
    );
  }

  const open = job?.status === "queued" || job?.status === "running";

  const grouped = KIND_ORDER.map((kind) => ({
    kind,
    items: findings.filter((finding) => finding.kind === kind),
  })).filter((group) => group.items.length > 0);

  return (
    <section className="card">
      <div className="card-head">
        <h2>
          Hunt{job?.targetDomain ? <span className="mono"> · {job.targetDomain}</span> : null}
        </h2>
        <span className={`chip hunt-${job?.status ?? "queued"}`}>
          {open && <span className="spinner" aria-hidden="true" />}
          {STATUS_LABEL[job?.status ?? "queued"]}
          {job ? ` · ${job.findingsCount} finding${job.findingsCount === 1 ? "" : "s"}` : ""}
        </span>
      </div>

      <div className="card-body">
        {/*
          `partial` names the enrichers that did not answer. A hunt that quietly returns less
          than usual, with no explanation, is indistinguishable from a company that simply
          has less to find — and those two need very different follow-up.
        */}
        {job?.errors && Object.keys(job.errors).length > 0 && (
          <div className="notice notice-warning">
            <strong>Some lookups did not complete.</strong>
            <ul>
              {Object.entries(job.errors).map(([name, message]) => (
                <li key={name}>
                  <span className="mono">{name}</span> — {message || "no response"}
                </li>
              ))}
            </ul>
          </div>
        )}

        {loading && findings.length === 0 ? (
          <div className="skeleton chart-box" />
        ) : grouped.length === 0 ? (
          <p className="muted">
            {open
              ? "Looking. Findings appear here as each lookup returns."
              : "Nothing found for this company."}
          </p>
        ) : (
          <div className="finding-groups">
            {grouped.map((group) => (
              <div key={group.kind} className="finding-group">
                <h3 title={KIND_HELP[group.kind]}>
                  {KIND_LABEL[group.kind]}
                  <span className="muted"> · {group.items.length}</span>
                </h3>
                <ul className="findings">
                  {group.items.map((finding) => (
                    <FindingRow key={finding.id} finding={finding} />
                  ))}
                </ul>
              </div>
            ))}
            {open && (
              <p className="muted">
                <span className="spinner" aria-hidden="true" /> Still looking — more may
                arrive.
              </p>
            )}
          </div>
        )}
      </div>
    </section>
  );
}

function FindingRow({ finding }: { finding: HuntFinding }) {
  const detail = finding.detail ?? {};

  return (
    <li className="finding">
      <div className="finding-head">
        <span className="finding-title">{finding.title}</span>
        <span className="chip chip-subtle" title="Where this finding came from.">
          {finding.sourceLabel}
        </span>
        {finding.occurredAt && (
          <span className="muted">{formatRelative(finding.occurredAt)}</span>
        )}
      </div>
      <FindingDetail kind={finding.kind} detail={detail} />
    </li>
  );
}

function FindingDetail({
  kind,
  detail,
}: {
  kind: HuntFindingKind;
  detail: Record<string, unknown>;
}) {
  const list = (value: unknown): string[] =>
    Array.isArray(value) ? value.filter((v): v is string => typeof v === "string") : [];

  switch (kind) {
    case "raw_page_mention": {
      // ts_headline marks the matched term with << >>. Splitting on those lets the term be
      // highlighted without dangerously setting raw HTML from crawled criminal pages.
      const excerpt = typeof detail.excerpt === "string" ? detail.excerpt : "";
      const parts = excerpt.split(/<<|>>/);
      return (
        <p className="finding-excerpt">
          {parts.map((part, index) =>
            index % 2 === 1 ? (
              <mark key={index}>{part}</mark>
            ) : (
              <span key={index}>{part}</span>
            ),
          )}
        </p>
      );
    }

    case "registration": {
      const contacts = (detail.contacts ?? {}) as Record<string, string[]>;
      const entries = Object.entries(contacts).filter(([, v]) => v?.length);
      if (entries.length === 0) return null;
      return (
        <div className="chips">
          {entries.map(([role, emails]) =>
            emails.map((email) => (
              <span key={`${role}-${email}`} className="chip chip-whois">
                <span className="chip-role">{role}</span>
                {email}
              </span>
            )),
          )}
        </div>
      );
    }

    case "infrastructure": {
      const candidates: Array<[string, string[]]> = [
        ["A", list(detail.a)],
        ["AAAA", list(detail.aaaa)],
        ["MX", list(detail.mx)],
        ["NS", list(detail.ns)],
      ];
      const rows = candidates.filter(([, values]) => values.length > 0);
      if (rows.length === 0) return null;
      return (
        <div className="chips">
          {rows.map(([label, values]) => (
            <span key={label} className="chip chip-dns">
              <span className="chip-role">{label}</span>
              {values.slice(0, 4).join(", ")}
              {values.length > 4 ? ` +${values.length - 4}` : ""}
            </span>
          ))}
        </div>
      );
    }

    case "certificate": {
      const subdomains = list(detail.subdomains);
      if (subdomains.length === 0) return null;
      return (
        <div className="chips">
          {subdomains.slice(0, 12).map((host) => (
            <span key={host} className="chip mono chip-subtle">
              {host}
            </span>
          ))}
          {subdomains.length > 12 && (
            <span className="muted">+{subdomains.length - 12} more</span>
          )}
        </div>
      );
    }

    case "liveness": {
      const technologies = list(detail.technologies);
      if (technologies.length === 0) return null;
      return (
        <div className="chips">
          {technologies.map((tech) => (
            <span key={tech} className="chip chip-tech">
              {tech}
            </span>
          ))}
        </div>
      );
    }

    default:
      return null;
  }
}
