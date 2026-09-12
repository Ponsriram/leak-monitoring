import { ExternalLink } from "../../components/ExternalLink";
import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { EmptyState, ErrorState } from "../../components/states";
import { LeakStatusChip } from "../../components/StatusChip";
import { TagChip } from "../../components/TagChip";
import { formatBytes, formatRelative } from "../../lib/format";
import {
  useHunt,
  useSearch,
  useStartHunt,
  type DomainHit,
  type HuntFinding,
  type HuntFindingKind,
  type SearchLeakHit,
} from "../../lib/queries";
import { HuntPanel } from "./HuntPanel";

/**
 * Company search.
 *
 * The page is built around the fact that there are two very different answers to "what do we
 * know about this company", arriving on completely different timescales. What we already
 * hold comes back in milliseconds and renders immediately. What we have to go and fetch
 * takes seconds, and appears underneath as it arrives.
 *
 * Presenting those as one blocking result would mean the common case — a company we already
 * have listings for — waits on a certificate-transparency lookup that has nothing to do with
 * it. So the instant half never waits for the hunt, and the hunt never blocks the page.
 */

/** Long enough that typing a company name is one request, short enough to feel immediate. */
const DEBOUNCE_MS = 300;

export function SearchPage() {
  // The query lives in the URL so a search is linkable — an analyst pasting a result to a
  // colleague is the normal way this gets used, and `?q=` costs nothing to support.
  const [params, setParams] = useSearchParams();
  const urlQuery = params.get("q") ?? "";

  const [input, setInput] = useState(urlQuery);
  const [debounced, setDebounced] = useState(urlQuery);
  const [huntId, setHuntId] = useState<number | null>(null);

  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    const timer = window.setTimeout(() => setDebounced(input.trim()), DEBOUNCE_MS);
    return () => window.clearTimeout(timer);
  }, [input]);

  // Keep the URL in step with what was actually searched, not with every keystroke.
  useEffect(() => {
    if (debounced === urlQuery) return;
    setParams(debounced ? { q: debounced } : {}, { replace: true });
  }, [debounced, urlQuery, setParams]);

  // A new search term invalidates whichever hunt was on screen — leaving the previous
  // company's findings under a different company's results is actively misleading.
  useEffect(() => {
    setHuntId(null);
  }, [debounced]);

  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  const search = useSearch(debounced);
  const startHunt = useStartHunt();
  const hunt = useHunt(huntId);

  const result = search.data;

  // A hunt already run for this query recently: offer it rather than re-running it.
  useEffect(() => {
    if (result?.hunt.existingJobId != null && huntId === null) {
      setHuntId(result.hunt.existingJobId);
    }
  }, [result?.hunt.existingJobId, huntId]);

  async function runHunt() {
    if (!debounced) return;
    const { job } = await startHunt.mutateAsync(debounced);
    setHuntId(job.id);
  }

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>Search</h1>
          <p className="page-sub">
            Search everything already collected, then go and fetch what we don&rsquo;t have.
          </p>
        </div>
      </div>

      <div className="search-bar">
        <span className="search-icon" aria-hidden="true">
          ⌕
        </span>
        <input
          ref={inputRef}
          type="search"
          className="search-input"
          placeholder="Company name or domain — e.g. Contoso, or contoso.com"
          value={input}
          onChange={(event) => setInput(event.target.value)}
          aria-label="Search companies and domains"
        />
      </div>

      {debounced.length < 2 ? (
        <EmptyState title="Type a company name or a domain">
          <p className="muted">
            Two characters is enough to start. Results come from what has already been
            collected; anything missing can be fetched on demand.
          </p>
        </EmptyState>
      ) : search.isError ? (
        <ErrorState error={search.error} onRetry={search.refetch} />
      ) : (
        <>
          {result && (
            <HuntPrompt
              reason={result.hunt.reason}
              suggested={result.hunt.suggested}
              hasHunt={huntId !== null}
              pending={startHunt.isPending}
              onRun={runHunt}
            />
          )}

          <section className="card">
            <div className="card-head">
              <h2>Listings</h2>
              {result && (
                <span className="muted">
                  {result.leaks.length === 0
                    ? "no matches"
                    : `${result.leaks.length} match${result.leaks.length === 1 ? "" : "es"}`}
                </span>
              )}
            </div>
            <div className="card-body">
              {search.isPending ? (
                <div className="skeleton chart-box" />
              ) : result && result.leaks.length > 0 ? (
                <LeakHits hits={result.leaks} />
              ) : (
                <EmptyState title="No listings for this company">
                  <p className="muted">
                    Nothing collected has named it yet. A hunt can still find registration,
                    infrastructure, and mentions in pages we crawled but never extracted.
                  </p>
                </EmptyState>
              )}
            </div>
          </section>

          {result && result.domains.length > 0 && (
            <section className="card">
              <div className="card-head">
                <h2>Domains</h2>
                <span className="muted">registration, stack and liveness</span>
              </div>
              <div className="card-body">
                <div className="domain-grid">
                  {result.domains.map((domain) => (
                    <DomainCard key={domain.domain} domain={domain} />
                  ))}
                </div>
              </div>
            </section>
          )}

          {huntId !== null && (
            <HuntPanel
              job={hunt.data?.job ?? null}
              findings={hunt.data?.findings ?? []}
              loading={hunt.isPending}
              error={hunt.error}
            />
          )}
        </>
      )}
    </div>
  );
}

function HuntPrompt({
  reason,
  suggested,
  hasHunt,
  pending,
  onRun,
}: {
  reason: string;
  suggested: boolean;
  hasHunt: boolean;
  pending: boolean;
  onRun: () => void;
}) {
  return (
    <div className={`hunt-prompt${suggested ? " hunt-prompt-suggested" : ""}`}>
      <div>
        <strong>{suggested ? "Worth looking further" : "Fetch fresh data"}</strong>
        <p className="muted">{reason}</p>
      </div>
      <button type="button" className="btn" onClick={onRun} disabled={pending}>
        {pending ? "Starting…" : hasHunt ? "Run again" : "Hunt this company"}
      </button>
    </div>
  );
}

function LeakHits({ hits }: { hits: SearchLeakHit[] }) {
  return (
    <div className="table-wrap">
      <table className="table">
        <thead>
          <tr>
            <th>Victim</th>
            <th>Actor</th>
            <th>Status</th>
            <th>Tags</th>
            <th>Size</th>
            <th>First seen</th>
          </tr>
        </thead>
        <tbody>
          {hits.map((hit) => (
            <tr key={hit.id}>
              <td>
                <div className="victim">
                  <Link to={`/dashboard/leaks?q=${encodeURIComponent(hit.victimName ?? hit.victimDomain ?? "")}`}>
                    {hit.victimName ?? hit.victimDomain ?? "—"}
                  </Link>
                  {hit.victimDomain && hit.victimName && (
                    <div className="mono small">
                      <ExternalLink value={hit.victimDomain} />
                    </div>
                  )}
                </div>
                {/*
                  A zero rank means the row matched on substring only — the full-text index
                  did not see it as a word. Surfacing that explains why a result that looks
                  like a weaker match is in the list at all, instead of it reading as noise.
                */}
                {hit.rank === 0 && (
                  <span className="chip chip-subtle" title="Matched on a substring of the name or domain, not on a whole word.">
                    partial match
                  </span>
                )}
              </td>
              <td>
                <span className="chip chip-actor">{hit.actorGroup}</span>
              </td>
              <td>
                <LeakStatusChip status={hit.status} />
              </td>
              <td>
                <div className="chips">
                  {hit.victimCountry && <TagChip kind="country" value={hit.victimCountry} />}
                  {hit.victimSector && <TagChip kind="sector" value={hit.victimSector} />}
                </div>
              </td>
              <td className="mono">{formatBytes(hit.leakSizeBytes)}</td>
              <td title={new Date(hit.firstSeenAt).toLocaleString()}>
                {formatRelative(hit.firstSeenAt)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

const SITE_STATUS_LABEL: Record<DomainHit["status"], string> = {
  live: "Live",
  down: "Down",
  error: "Probe failed",
  not_scanned: "Not scanned",
};

const WHOIS_ROLE_LABEL: Record<string, string> = {
  registrarAbuse: "Registrar abuse",
  registrant: "Registrant",
  admin: "Admin",
  tech: "Tech",
  billing: "Billing",
};

function DomainCard({ domain }: { domain: DomainHit }) {
  const contacts = useMemo(
    () => Object.entries(domain.whoisContacts ?? {}).filter(([, list]) => list?.length),
    [domain.whoisContacts],
  );

  return (
    <article className="domain-card">
      <header className="domain-card-head">
        <div>
          <div className="mono domain-name">{domain.domain}</div>
          {domain.pageTitle && <div className="muted">{domain.pageTitle}</div>}
        </div>
        <span className={`chip site-${domain.status}`}>
          {SITE_STATUS_LABEL[domain.status]}
          {domain.httpStatus ? ` · ${domain.httpStatus}` : ""}
        </span>
      </header>

      {domain.registrar && (
        <div className="domain-row">
          <span className="domain-label">Registrar</span>
          <span>{domain.registrar}</span>
        </div>
      )}

      {contacts.length > 0 && (
        <div className="domain-row">
          <span className="domain-label">WHOIS</span>
          <div className="chips">
            {contacts.map(([role, emails]) =>
              (emails ?? []).map((email) => (
                <span key={`${role}-${email}`} className="chip chip-whois">
                  <span className="chip-role">{WHOIS_ROLE_LABEL[role] ?? role}</span>
                  {email}
                </span>
              )),
            )}
          </div>
        </div>
      )}

      {domain.technologies && domain.technologies.length > 0 && (
        <div className="domain-row">
          <span className="domain-label">Stack</span>
          <div className="chips">
            {domain.technologies.map((tech) => (
              <span key={tech} className="chip chip-tech">
                {tech}
              </span>
            ))}
          </div>
        </div>
      )}

      {domain.checkedAt && (
        <footer className="domain-card-foot muted">
          {/*
            Always shown, because every field above is a cached assertion about a moment in
            the past. "Live" with no timestamp invites reading it as "live right now", which
            is the one thing a cache cannot promise.
          */}
          Checked {formatRelative(domain.checkedAt)}
        </footer>
      )}
    </article>
  );
}

export type { HuntFinding, HuntFindingKind };
