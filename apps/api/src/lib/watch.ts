/**
 * Turning what someone typed into the value a watch entry is stored and matched as.
 *
 * Normalised here, once, because the unique index on `(kind, value)` only means "one entry per
 * thing" if equivalent inputs produce the same value: `ACME.com`, `https://www.acme.com/login`
 * and `ops@acme.com` are all the domain `acme.com`, and storing three entries would triple
 * every match and every alert that is built on them later.
 */

// Labels of 1-63 characters, then a TLD. Punycode TLDs (`xn--…`) are legal and in real use.
const DOMAIN = /^(?=.{4,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+(?:[a-z]{2,24}|xn--[a-z0-9-]{2,20})$/;

/**
 * Below this a keyword matches so much it means nothing: "ab" is inside half of all victim
 * names. Three still lets a short brand through ("IBM", "SAP").
 */
export const MIN_KEYWORD_LENGTH = 3;
export const MAX_KEYWORD_LENGTH = 100;

/** The registrable-looking domain inside a URL, address or bare host. Null if there isn't one. */
export function normalizeDomain(raw: string): string | null {
  let value = raw.trim().toLowerCase();
  value = value.replace(/^[a-z][a-z0-9+.-]*:\/\//, ""); // scheme
  value = value.replace(/^[^@/?#]*@/, ""); // userinfo, which is also how `ops@acme.com` reduces
  value = value.split(/[/?#]/)[0] ?? "";
  value = value.replace(/:\d+$/, "").replace(/^www\./, "").replace(/\.$/, "");
  return DOMAIN.test(value) ? value : null;
}

/** A keyword, trimmed, lower-cased and with runs of whitespace collapsed. Null if unusable. */
export function normalizeKeyword(raw: string): string | null {
  const value = raw.trim().toLowerCase().replace(/\s+/g, " ");
  if (value.length < MIN_KEYWORD_LENGTH || value.length > MAX_KEYWORD_LENGTH) return null;
  // Punctuation alone ("---", "...") would match every row that contains it.
  if (!/[\p{L}\p{N}]/u.test(value)) return null;
  return value;
}

export function normalizeWatchValue(kind: "domain" | "keyword", raw: string): string | null {
  return kind === "domain" ? normalizeDomain(raw) : normalizeKeyword(raw);
}
