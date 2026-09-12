import { CopyButton } from "./CopyButton";

/**
 * A victim domain, rendered as a link out to the site.
 *
 * These are the sites of ransomware victims and frequently still compromised, so the anchor
 * carries `nofollow` alongside `noopener noreferrer`: no referrer leaks this console's URL to
 * a host that may be attacker-controlled, and no ranking signal is passed to it. The copy
 * button sits beside it precisely so the value can be taken without visiting the site.
 *
 * Stored values are bare hostnames far more often than absolute URLs, so the scheme is
 * prepended rather than assumed — an href of `example.com` resolves relative to the console
 * and navigates to a dashboard route that does not exist.
 */

/** Longer than this and the label wraps a table column out of shape. */
const MAX_LABEL = 40;

function toHref(value: string): string {
  return /^[a-z][a-z0-9+.-]*:\/\//i.test(value) ? value : `https://${value}`;
}

function truncate(value: string): string {
  return value.length > MAX_LABEL ? `${value.slice(0, MAX_LABEL - 1)}…` : value;
}

export function ExternalLink({
  value,
  className = "",
  copy = true,
}: {
  value: string;
  className?: string;
  /** Off where the same value already has a copy button beside it. */
  copy?: boolean;
}) {
  const href = toHref(value);
  const label = truncate(value);

  return (
    <span className={`ext-link-wrap ${className}`.trim()}>
      <a
        className="ext-link"
        href={href}
        target="_blank"
        rel="noopener noreferrer nofollow"
        // The full value, because the visible label may be an ellipsis of it.
        title={href}
        // Expandable rows toggle on click; following a link must not also expand the row.
        onClick={(event) => event.stopPropagation()}
      >
        <span className="ext-link-label">{label}</span>
        <svg
          className="ext-link-icon"
          viewBox="0 0 12 12"
          width="10"
          height="10"
          aria-hidden="true"
          focusable="false"
        >
          <path
            d="M4.5 1.5h6v6M10.5 1.5 5 7M8 8.5v2h-6.5V4h2"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.3"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </svg>
      </a>
      {copy && <CopyButton value={value} label="Copy domain" />}
    </span>
  );
}
