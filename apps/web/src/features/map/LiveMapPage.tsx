import L from "leaflet";
import "leaflet/dist/leaflet.css";
import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { ErrorState } from "../../components/states";
import { coordsFor, flagFor } from "../../lib/country-coords";
import { formatNumber } from "../../lib/format";
import { useLiveMap, type MapActor, type MapPeriod } from "../../lib/incidents";
import { WORLD_LAND } from "../../lib/world-geometry";

/**
 * The live threat map.
 *
 * Two things on screen: where victims are, and who is hitting them. The map carries the
 * first, the actor rail the second, and selecting an actor filters the map to just that
 * group's countries — which is the only interaction that makes 84 overlapping arcs legible.
 *
 * **What the arcs mean, and what they do not.** An arc joins two countries the *same actor*
 * hit inside the window. It is not an attack path and does not point from attacker to
 * victim: we know which victims a group listed and we do not know where that group operates
 * from. Drawing a line out of a supposed home country would be inventing attribution, which
 * is the one thing this console must never do. So the arcs show campaign spread — a fact we
 * actually hold.
 *
 * Leaflet is imperative and mutable, so the map instance lives in a ref outside React's
 * render cycle and its layers are redrawn in an effect when the data or selection changes.
 */

/** One colour per actor, assigned by position so a given actor keeps its colour in a window. */
const ACTOR_COLOURS = [
  "#e0564f",
  "#2a78d6",
  "#8b5cf6",
  "#0ca35f",
  "#f0932b",
  "#d63384",
  "#0dcaf0",
  "#7c5cff",
  "#e8b931",
  "#14a0a0",
];

function todayIso(): string {
  return new Date().toISOString().slice(0, 10);
}

/**
 * A curved polyline between two points.
 *
 * Leaflet draws straight lines. Straight lines between country centroids overlap into an
 * unreadable mesh once there are more than a dozen, and offer no way to tell which end of a
 * crossing pair belongs to which pair. Bowing each one perpendicular to its own midpoint
 * separates them, and the bow scales with distance so short hops stay nearly straight.
 */
function arcPoints(
  from: [number, number],
  to: [number, number],
  segments = 24,
): [number, number][] {
  const [lat1, lng1] = from;
  const [lat2, lng2] = to;

  const midLat = (lat1 + lat2) / 2;
  const midLng = (lng1 + lng2) / 2;

  const dLat = lat2 - lat1;
  const dLng = lng2 - lng1;
  const distance = Math.sqrt(dLat * dLat + dLng * dLng);

  // Perpendicular offset, capped so a half-globe arc does not bow off the top of the map.
  const bow = Math.min(distance * 0.22, 28);
  const norm = distance || 1;
  const controlLat = midLat + (-dLng / norm) * bow;
  const controlLng = midLng + (dLat / norm) * bow;

  const points: [number, number][] = [];
  for (let i = 0; i <= segments; i += 1) {
    const t = i / segments;
    const inverse = 1 - t;
    // Quadratic Bézier.
    const lat = inverse * inverse * lat1 + 2 * inverse * t * controlLat + t * t * lat2;
    const lng = inverse * inverse * lng1 + 2 * inverse * t * controlLng + t * t * lng2;
    points.push([lat, lng]);
  }
  return points;
}

export function LiveMapPage() {
  const navigate = useNavigate();

  const [period, setPeriod] = useState<MapPeriod>("date");
  const [date, setDate] = useState<string>(todayIso());
  const [selected, setSelected] = useState<string | null>(null);

  const query = useLiveMap(period, date);
  const data = query.data;

  const containerRef = useRef<HTMLDivElement | null>(null);
  const mapRef = useRef<L.Map | null>(null);
  const layerRef = useRef<L.LayerGroup | null>(null);

  const colourOf = useMemo(() => {
    const map = new Map<string, string>();
    (data?.actors ?? []).forEach((actor, index) => {
      map.set(actor.actorGroup, ACTOR_COLOURS[index % ACTOR_COLOURS.length]!);
    });
    return (actorGroup: string) => map.get(actorGroup) ?? "#8798a0";
  }, [data?.actors]);

  // Create the map once, when the container is in the DOM.
  useEffect(() => {
    if (mapRef.current || !containerRef.current) return;

    const map = L.map(containerRef.current, {
      center: [25, 10],
      zoom: 2,
      minZoom: 2,
      maxZoom: 7,
      worldCopyJump: true,
      attributionControl: false,
      // The map is a canvas for the data, not something to browse. Dragging and zooming stay
      // on; the rest of Leaflet's chrome would only compete with the actor rail.
      zoomControl: true,
    });

    /**
     * Land, from a vendored outline rather than a tile layer.
     *
     * Street tiles carry roads and labels at a zoom where the data is country-scale, cost a
     * third-party request per pane, and make the map depend on someone else's service being
     * up. This is one constant in the bundle, renders offline, and is styled to stay in the
     * background — the data is the foreground, and a busy basemap competes with it.
     *
     * `interactive: false` so land never swallows a click meant for a marker.
     */
    L.geoJSON(WORLD_LAND, {
      interactive: false,
      // Styled by class, not by colour options. Leaflet writes `color`/`fillColor` onto the
      // SVG path as presentation attributes, where a `var(--token)` never resolves — the land
      // would silently render as nothing. A class lets the stylesheet own it, which is also
      // what makes the map follow the light/dark theme without a second colour table here.
      style: () => ({ className: "map-land", weight: 0.5, fillOpacity: 1 }),
    }).addTo(map);

    mapRef.current = map;
    layerRef.current = L.layerGroup().addTo(map);

    return () => {
      map.remove();
      mapRef.current = null;
      layerRef.current = null;
    };
  }, []);

  // Redraw markers and arcs whenever the data or the selection changes.
  useEffect(() => {
    const layer = layerRef.current;
    if (!layer || !data) return;

    layer.clearLayers();

    const visible = (actorGroup: string) => selected === null || selected === actorGroup;

    for (const arc of data.arcs) {
      if (!visible(arc.actorGroup)) continue;
      const from = coordsFor(arc.from);
      const to = coordsFor(arc.to);
      if (!from || !to) continue;

      L.polyline(arcPoints(from, to), {
        color: colourOf(arc.actorGroup),
        weight: 1.1,
        opacity: selected ? 0.85 : 0.42,
        dashArray: "5 6",
        interactive: false,
      }).addTo(layer);
    }

    for (const point of data.points) {
      const actorsHere = point.actors.filter(visible);
      if (actorsHere.length === 0) continue;

      const coords = coordsFor(point.country);
      if (!coords) continue;

      const colour = colourOf(actorsHere[0]!);
      // Radius grows with the log of the count, not linearly: the United States has sixty
      // times Malta's incidents and a linear radius would draw a disc covering Europe.
      const radius = 5 + Math.min(9, Math.log2(point.total + 1) * 2.2);

      L.circleMarker(coords, {
        radius,
        color: colour,
        weight: 2,
        fillColor: colour,
        fillOpacity: 0.35,
      })
        .bindTooltip(
          `<strong>${flagFor(point.country) ?? ""} ${point.country}</strong><br/>` +
            `${point.total} incident${point.total === 1 ? "" : "s"}<br/>` +
            `<span class="muted">${actorsHere.slice(0, 4).join(", ")}` +
            `${actorsHere.length > 4 ? ` +${actorsHere.length - 4}` : ""}</span>`,
          { direction: "top", opacity: 1 },
        )
        .addTo(layer);
    }
  }, [data, selected, colourOf]);

  return (
    <div className="map-page">
      <header className="map-head">
        <button type="button" className="btn btn-sm" onClick={() => navigate(-1)}>
          ← Back
        </button>

        <div className="map-title">
          <h1>
            <span aria-hidden="true">◉</span> Live Threat Map
          </h1>
          <p className="muted small">
            {period === "date" ? "Date" : period === "week" ? "Week" : "Month"} · {date} ·{" "}
            {formatNumber(data?.totals.actors ?? 0)} actors ·{" "}
            {formatNumber(data?.totals.incidents ?? 0)} incidents
          </p>
        </div>

        <div className="map-controls">
          <div className="segmented" role="group" aria-label="Time window">
            {(["date", "week", "month"] as MapPeriod[]).map((value) => (
              <button
                key={value}
                type="button"
                className={period === value ? "active" : undefined}
                onClick={() => setPeriod(value)}
                aria-pressed={period === value}
              >
                {value === "date" ? "Date" : value === "week" ? "Week" : "Month"}
              </button>
            ))}
          </div>
          <input
            type="date"
            className="filter-input date-input"
            value={date}
            max={todayIso()}
            onChange={(event) => setDate(event.target.value || todayIso())}
            aria-label="Anchor date"
          />
        </div>
      </header>

      <div className="map-body">
        <div className="map-canvas">
          <div ref={containerRef} className="leaflet-host" />
          {query.isError && (
            <div className="map-overlay">
              <ErrorState error={query.error} onRetry={query.refetch} />
            </div>
          )}
          {!query.isError && data && data.points.length === 0 && (
            <div className="map-overlay">
              <div className="state">
                <div className="state-title">Nothing in this window</div>
                <p className="muted">
                  No incidents carried a country between these dates. Try Week or Month.
                </p>
              </div>
            </div>
          )}
          {selected && (
            <button
              type="button"
              className="btn btn-sm map-clear"
              onClick={() => setSelected(null)}
            >
              Clear filter · {selected}
            </button>
          )}
        </div>

        <aside className="actor-rail">
          <header className="actor-rail-head">
            <span>
              <span aria-hidden="true">▦</span> Active threat actors
            </span>
            <span className="muted">{data?.actors.length ?? 0}</span>
          </header>

          <div className="actor-list">
            {query.isPending && <div className="skeleton chart-box" />}
            {(data?.actors ?? []).map((actor) => (
              <ActorCard
                key={actor.actorGroup}
                actor={actor}
                colour={colourOf(actor.actorGroup)}
                selected={selected === actor.actorGroup}
                onSelect={() =>
                  setSelected((current) =>
                    current === actor.actorGroup ? null : actor.actorGroup,
                  )
                }
              />
            ))}
            {data && data.actors.length === 0 && !query.isPending && (
              <p className="muted">No actors active in this window.</p>
            )}
          </div>
        </aside>
      </div>
    </div>
  );
}

/**
 * The four counters.
 *
 * `TECH` is a real count of distinct technologies fingerprinted across that actor's victims.
 * The other three read zero because there is no indicator or vulnerability feed wired up
 * yet — so the tooltip says exactly that. A zero that means "we have no source" and a zero
 * that means "we looked and found none" are different claims, and a bare 0 makes them
 * indistinguishable.
 */
const COUNTER_HELP: Record<string, string> = {
  iocs: "Indicators of compromise linked to this actor. No indicator feed is connected yet.",
  cves: "Vulnerabilities linked to this actor. No vulnerability feed is connected yet.",
  ttps: "MITRE ATT&CK techniques for this actor. No technique mapping is connected yet.",
  tech: "Distinct web technologies fingerprinted across this actor's victims.",
};

function ActorCard({
  actor,
  colour,
  selected,
  onSelect,
}: {
  actor: MapActor;
  colour: string;
  selected: boolean;
  onSelect: () => void;
}) {
  const counters: [string, number][] = [
    ["iocs", actor.counts.iocs],
    ["cves", actor.counts.cves],
    ["ttps", actor.counts.ttps],
    ["tech", actor.counts.tech],
  ];

  return (
    <button
      type="button"
      className={`actor-card${selected ? " actor-card-selected" : ""}`}
      onClick={onSelect}
      aria-pressed={selected}
    >
      <div className="actor-card-head">
        <span className="actor-swatch" style={{ background: colour }} aria-hidden="true" />
        <div className="actor-ident">
          <span className="actor-name">{actor.actorGroup}</span>
          <div className="chips">
            {actor.categories.map((category) => (
              <span key={category} className="chip chip-category">
                {category}
              </span>
            ))}
            <span className="muted small">
              {actor.targets} target{actor.targets === 1 ? "" : "s"}
            </span>
          </div>
        </div>
      </div>

      <div className="actor-counters">
        {counters.map(([label, value]) => (
          <span
            key={label}
            className={`counter${value === 0 ? " counter-zero" : ""}`}
            title={COUNTER_HELP[label]}
          >
            <span className="counter-value">{value}</span>
            <span className="counter-label">{label}</span>
          </span>
        ))}
      </div>
    </button>
  );
}
