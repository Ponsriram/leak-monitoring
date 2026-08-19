/**
 * The NER tags: where a victim is, and what it does.
 *
 * Both are inferred, never stated by the source in a structured form — a country comes from
 * a gazetteer match on the listing text or from the domain's ccTLD, a sector from words in
 * the victim's own name. So each chip carries a title explaining where the value came from.
 * A tag that reads as authoritative when it is a heuristic is how a filter quietly starts
 * excluding real results.
 *
 * Rendered with an icon *and* the value, never the icon alone: "🌍 Germany" survives being
 * read aloud, copied into a ticket, or viewed by someone whose emoji font renders neither.
 */

type Kind = "country" | "sector";

const ICON: Record<Kind, string> = {
  country: "◎",
  sector: "▤",
};

const HELP: Record<Kind, string> = {
  country:
    "Location, extracted from the listing text or inferred from the victim's country-code domain.",
  sector:
    "Industry, inferred from words in the victim's name and the listing around it.",
};

export function TagChip({ kind, value }: { kind: Kind; value: string }) {
  return (
    <span className={`chip tag tag-${kind}`} title={`${HELP[kind]} (${value})`}>
      <span className="tag-icon" aria-hidden="true">
        {ICON[kind]}
      </span>
      {value}
    </span>
  );
}
