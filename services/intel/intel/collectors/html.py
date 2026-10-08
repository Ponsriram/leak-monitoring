"""HTML → clean text, with selectolax.

selectolax parses roughly 10–30× faster than BeautifulSoup. That matters here because the
pipeline re-parses on every crawl.

A listing page whose victims sit in repeated tiles, cards or rows keeps those boundaries in
its text: each tile's text is emitted on its own, between `BLOCK_BREAK` lines, so extraction
can treat one tile as one victim (see `listing_blocks` and `pipeline.extract_page`). A page
with no repeated structure comes out exactly as it always did.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterator

from selectolax.parser import HTMLParser, Node

from ..extract.gazetteer import CCTLD_COUNTRY, parse_country

# Elements that never contain listing data but do contain a lot of text.
_STRIP_TAGS = ("script", "style", "noscript", "svg", "iframe", "head")

# Elements whose content changes on every request (live countdowns, view counters). They are
# removed before hashing so a ticking clock doesn't make every page look "changed" and
# trigger a pointless re-extraction each cycle.
_VOLATILE_SELECTORS = (
    ".countdown",
    "[class*='countdown']",
    "[class*='timer']",
    "[id*='countdown']",
    "[id*='timer']",
)

# A clock renders as a few characters ("2h 39m", "4 D 15 H 34 M 21 S"). A matched element
# holding more text than this is a container that merely carries the word — direwolf marks
# every victim card `card countdown-active` — and removing it deleted the whole listing.
_VOLATILE_MAX_CHARS = 40

# --- listing blocks ---------------------------------------------------------------------------

# (tag, sorted class list): what makes two elements "the same kind of tile".
Signature = tuple[str, tuple[str, ...]]

# The one character that marks a block boundary in page text, and the line it forms. Defined
# here and nowhere else: `to_text` writes it, `pipeline.split_blocks` reads it. U+241E (SYMBOL
# FOR RECORD SEPARATOR) is printable, so `_clean` would keep it, and no leak site prints it —
# but `_clean` strips it from page text anyway, so a page cannot fake a boundary.
RECORD_SEPARATOR = "␞"
BLOCK_BREAK = f"\n{RECORD_SEPARATOR}\n"

# Page chrome that repeats (menus, link rows) but never holds the listing.
_CHROME_TAGS = frozenset({"nav", "header", "footer"})

# A listing is at least this many same-shaped siblings, each with at least this much text. A
# nav bar's short links fail the second test; a pair of columns fails the first.
_MIN_BLOCKS = 3
_MIN_BLOCK_CHARS = 15

# The blocks must hold this share of the page's text. Below it, the repeated structure is a
# sidebar or a "recent posts" box beside a listing laid out some other way, and splitting on
# it would throw the real listing away as "text outside the blocks".
_MIN_COVERAGE = 0.35

# Descending from a list of wrappers to the items inside them (a grid's rows to its tiles):
# the items must account for nearly all of each wrapper's text, render as at least two lines
# each, and mostly not open with a field label. The last two tests are what keep a card's own
# fields — "Revenue: $5M", "Industry: Retail" — from being mistaken for a list of tiles.
_WRAPPER_COVERAGE = 0.9
_MIN_ITEM_LINES = 2
_FIELD_LABEL = re.compile(r"^[^\W\d][\w .'&/-]{0,30}:")
# Tags that hold a piece of a record, never a whole one.
_PART_TAGS = frozenset(
    {"p", "span", "b", "strong", "em", "i", "small", "label", "td", "th", "br"}
    | {f"h{n}" for n in range(1, 7)}
)

# Country flags rendered as markup rather than text: `<img alt="Germany">`,
# `<img src="/flags/de.png">`, `<span class="fi fi-de">`.
_FLAG_CLASS = re.compile(r"^(?:fi|flag|flag-icon|country)-([a-z]{2})$", re.I)
_FLAG_SRC = re.compile(r"flags?/(?:[\w-]+/)*([a-z]{2})\.(?:svg|png|gif|webp|jpe?g)$", re.I)


def to_text(
    html: str,
    *,
    drop_volatile: bool = True,
    segment: bool = True,
    item_selector: str | None = None,
) -> str:
    """Extract readable text from a page.

    With `segment`, a page whose victims sit in repeated blocks is emitted as the text before
    the blocks, each block's text, then the text after, joined by `BLOCK_BREAK` — so the
    result always splits into `[before, block, …, block, after]`. `item_selector` (a CSS
    selector, set per source) names the blocks instead of detecting them. A page with no
    blocks is returned exactly as `segment=False` would return it.
    """
    tree = HTMLParser(html)

    for tag in _STRIP_TAGS:
        for node in tree.css(tag):
            node.decompose()

    if drop_volatile:
        for selector in _VOLATILE_SELECTORS:
            try:
                for node in tree.css(selector):
                    if len(node.text(strip=True)) <= _VOLATILE_MAX_CHARS:
                        node.decompose()
            except Exception:  # noqa: BLE001 - selectolax raises on odd selectors
                continue

    body = tree.body or tree.root
    if body is None:
        return ""

    blocks = _select_blocks(body, item_selector) if segment else []
    if not blocks:
        return _clean(body.text(separator="\n", strip=True))
    return _segmented_text(body, blocks)


def listing_blocks(html: str, *, item_selector: str | None = None) -> list[Node]:
    """The repeated elements a listing page lays its victims out in, or [] if it has none.

    Detection: the element whose direct children mostly share one signature (tag + sorted
    class list), with at least three of them holding real text. Every such parent is scored
    by the total text of its matching children and the highest wins, so a nav menu or a
    footer's link row loses to the listing. `item_selector` replaces detection outright.
    """
    tree = HTMLParser(html)
    for tag in _STRIP_TAGS:
        for node in tree.css(tag):
            node.decompose()
    body = tree.body or tree.root
    if body is None:
        return []
    return _select_blocks(body, item_selector)


def _select_blocks(root: Node, item_selector: str | None) -> list[Node]:
    if item_selector:
        return _selected_blocks(root, item_selector)
    return _detect_blocks(root)


def _selected_blocks(root: Node, selector: str) -> list[Node]:
    """The elements `selector` matches, outermost only, in page order."""
    try:
        matched = [node for node in root.css(selector) if _text_len(node)]
    except Exception:  # noqa: BLE001 - a bad selector in sources.yaml must not fail a page
        return []
    ids = {node.mem_id for node in matched}
    return [node for node in matched if not any(a.mem_id in ids for a in _ancestors(node))]


def _detect_blocks(root: Node) -> list[Node]:
    total = _text_len(root)
    if not total:
        return []

    best: list[Node] = []
    best_score = 0
    for parent in _elements_outside_chrome(root):
        members, score, _ = _repeated_children(parent, min_count=_MIN_BLOCKS)
        if score > best_score:
            best, best_score = members, score

    if not best or best_score < total * _MIN_COVERAGE:
        return []

    # A grid's rows are a list too, and score as highly as the tiles inside them. Step down
    # while each block is only a wrapper around more of the same.
    while (inner := _items_inside_wrappers(best)) is not None:
        best = inner
    return best


def _elements_outside_chrome(root: Node) -> Iterator[Node]:
    """Every element below `root`, without descending into nav, header or footer."""
    stack = [root]
    while stack:
        node = stack.pop()
        yield node
        children = [c for c in _element_children(node) if c.tag not in _CHROME_TAGS]
        stack.extend(reversed(children))


def _repeated_children(parent: Node, *, min_count: int) -> tuple[list[Node], int, Signature | None]:
    """`parent`'s children that share the dominant signature, their total text, and it.

    ([], 0, None) unless at least `min_count` of them hold `_MIN_BLOCK_CHARS` of text and they
    make up at least half of the children that hold any text at all.
    """
    children = [c for c in _element_children(parent) if c.tag not in _CHROME_TAGS]
    if len(children) < min_count:
        return [], 0, None

    lengths = {c.mem_id: _text_len(c) for c in children}
    with_text = [c for c in children if lengths[c.mem_id] and not _is_header_row(c)]

    groups: dict[Signature, list[Node]] = defaultdict(list)
    for child in with_text:
        groups[_signature(child)].append(child)
    if not groups:
        return [], 0, None

    def strength(item: tuple[Signature, list[Node]]) -> tuple[int, int]:
        nodes = item[1]
        full = sum(1 for n in nodes if lengths[n.mem_id] >= _MIN_BLOCK_CHARS)
        return full, sum(lengths[n.mem_id] for n in nodes)

    signature, _ = max(groups.items(), key=strength)
    members = [c for c in with_text if _matches(c, signature)]

    full = sum(1 for n in members if lengths[n.mem_id] >= _MIN_BLOCK_CHARS)
    if full < min_count or len(members) * 2 < len(with_text):
        return [], 0, None
    return members, sum(lengths[n.mem_id] for n in members), signature


def _matches(node: Node, signature: Signature) -> bool:
    """Same tag and classes — or a variant of them.

    Variants of one tile ("tile tile--new", "card active") keep the base classes and add one.
    They count only when there are base classes to share: a bare <div> beside bare <div>
    tiles may be anything.
    """
    tag, classes = signature
    own = _signature(node)
    if own == signature:
        return True
    return bool(classes) and node.tag == tag and set(classes) <= set(own[1])


def _items_inside_wrappers(blocks: list[Node]) -> list[Node] | None:
    """The tiles inside a list of row wrappers, or None if these blocks are the tiles.

    Each block must come down (through single-child wrappers) to a run of same-signature
    items covering nearly all of its text — or be a chain ending in a single such item, as a
    grid's last row often is. The signature must be the same in every block, there must be
    more items than blocks, and the items must each look like a record, not a field of one.
    """
    found: list[tuple[list[Node], list[Node], Signature | None]] = []
    for block in blocks:
        chain = list(_chain(block))
        members, score, signature = _repeated_children(chain[-1], min_count=1)
        if score < _text_len(block) * _WRAPPER_COVERAGE:
            members, signature = [], None
        found.append((chain, members, signature))

    signatures = {signature for _, members, signature in found if len(members) >= 2}
    if len(signatures) != 1:
        return None
    signature = signatures.pop()
    if signature is None or signature[0] in _PART_TAGS:
        return None

    items: list[Node] = []
    for chain, members, own in found:
        if own == signature:
            items.extend(members)
            continue
        single = next((node for node in chain if _matches(node, signature)), None)
        if single is None:
            return None
        items.append(single)

    if len(items) <= len(blocks):
        return None
    texts = [item.text(separator="\n", strip=True) for item in items]
    if sum(len(text.splitlines()) for text in texts) < _MIN_ITEM_LINES * len(items):
        return None
    if sum(1 for text in texts if _FIELD_LABEL.match(text)) * 2 >= len(items):
        return None
    return items


def _chain(node: Node) -> Iterator[Node]:
    """`node`, then down through every wrapper that has a single child holding text."""
    yield node
    while True:
        children = [c for c in _element_children(node) if _text_len(c)]
        if len(children) != 1:
            return
        node = children[0]
        yield node


def _segmented_text(body: Node, blocks: list[Node]) -> str:
    rendered = [_block_text(block) for block in blocks]

    # Each block is swapped for a marker, so the page's own text before, between and after
    # the blocks comes out of one `text()` call in reading order. The marker is private-use
    # characters, which `_clean` would never keep and no page renders.
    marker = ""
    for block in blocks:
        block.replace_with(marker)
    parts = body.text(separator="\n", strip=True).split(marker)

    before = _clean(parts[0])
    # Text between two blocks (an ad, a divider) is kept, after the list: it is still page
    # text that mirror and exposure scanning should see, but it is not a block.
    after = _clean("\n".join(parts[1:]))
    return BLOCK_BREAK.join([before, *(text for text in rendered if text), after])


def _block_text(block: Node) -> str:
    text = _clean(block.text(separator="\n", strip=True))
    if not text:
        return ""
    flags = [country for country in _flag_countries(block) if country.lower() not in text.lower()]
    return "\n".join([text, *flags])


def _flag_countries(block: Node) -> list[str]:
    """Countries a block shows as a flag image or icon class rather than as text."""
    found: list[str] = []
    # `css("*")`, not `traverse()`: on a node that is not the root, selectolax's traverse
    # carries on past the node's own subtree into the rest of the page.
    for node in [block, *block.css("*")]:
        attrs = node.attributes
        candidates: list[str | None] = []
        if node.tag == "img":
            candidates += [attrs.get("alt"), attrs.get("title")]
            src = _FLAG_SRC.search(attrs.get("src") or "")
            candidates.append(CCTLD_COUNTRY.get(src.group(1).lower()) if src else None)
        for token in (attrs.get("class") or "").split():
            code = _FLAG_CLASS.match(token)
            if code:
                candidates.append(CCTLD_COUNTRY.get(code.group(1).lower()))
        for raw in candidates:
            country = _country_of(raw)
            if country and country not in found:
                found.append(country)
    return found


def _country_of(label: str | None) -> str | None:
    """A flag's alt text or code -> a country name. Long alt text is a caption, not a flag."""
    label = (label or "").strip()
    if len(label) == 2:
        return CCTLD_COUNTRY.get(label.lower())
    if 2 < len(label) <= 40:
        return parse_country(label)
    return None


def _element_children(node: Node) -> Iterator[Node]:
    for child in node.iter(include_text=False):
        if child.tag and child.tag[0] not in "-_":
            yield child


def _ancestors(node: Node) -> Iterator[Node]:
    parent = node.parent
    while parent is not None:
        yield parent
        parent = parent.parent


def _signature(node: Node) -> Signature:
    return node.tag or "", tuple(sorted((node.attributes.get("class") or "").split()))


def _is_header_row(node: Node) -> bool:
    """A table row of nothing but <th> cells: the column headings, not a listing."""
    if node.tag != "tr":
        return False
    cells = [c.tag for c in _element_children(node)]
    return bool(cells) and all(tag == "th" for tag in cells)


def _text_len(node: Node) -> int:
    return len(node.text(separator=" ", strip=True))


def _clean(text: str) -> str:
    """Drop non-printable and non-ASCII noise, collapse blank runs.

    Keep printable Unicode — an ASCII-only filter would turn "Nestlé" into "Nestl" — and
    drop only control characters, the decorative symbols these sites are full of, and the
    record separator, which only `to_text` may write.
    """
    lines: list[str] = []
    for raw_line in text.splitlines():
        line = "".join(
            char
            for char in raw_line
            if char.isprintable()
            and not (0x2500 <= ord(char) <= 0x2BFF)
            and char != RECORD_SEPARATOR
        ).strip()
        if line:
            lines.append(line)

    # Collapse repeated blank lines that survived stripping.
    return "\n".join(lines)
