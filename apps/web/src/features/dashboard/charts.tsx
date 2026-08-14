import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  LabelList,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { formatNumber } from "../../lib/format";

/**
 * Both charts show a single measure, so neither uses the categorical palette: one series
 * colour (`--series-1`, validated against both surfaces) carries "leak volume" consistently
 * across the two views.
 *
 * Colouring the group bars individually would be colour-by-rank — the hue would encode
 * nothing the bar length doesn't already say, and it would repaint when a filter changes
 * the ordering. A single hue plus direct labels is the correct encoding.
 *
 * A single series needs no legend; each card's title names the measure.
 */

type TooltipEntry = { value?: number | string };
type TipProps = {
  active?: boolean;
  payload?: TooltipEntry[];
  label?: string | number;
};

function Tip({ active, payload, label, suffix }: TipProps & { suffix: string }) {
  if (!active || !payload?.length) return null;
  return (
    <div className="tooltip">
      <div className="tooltip-label">{label}</div>
      <div className="tooltip-value">
        {formatNumber(Number(payload[0]?.value ?? 0))} {suffix}
      </div>
    </div>
  );
}

/** Trailing daily volume. Zero-filled server-side, so gaps are real zeroes, not missing data. */
export function LeaksPerDayChart({ data }: { data: { date: string; total: number }[] }) {
  return (
    <div className="chart-box">
      <ResponsiveContainer width="100%" height="100%">
        <AreaChart data={data} margin={{ top: 8, right: 12, bottom: 0, left: -18 }}>
          <defs>
            <linearGradient id="leakFill" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor="var(--series-1)" stopOpacity={0.26} />
              <stop offset="100%" stopColor="var(--series-1)" stopOpacity={0.02} />
            </linearGradient>
          </defs>

          {/* Recessive grid: horizontal only, so it reads as a reference not a cage. */}
          <CartesianGrid stroke="var(--grid)" vertical={false} />

          <XAxis
            dataKey="date"
            tickLine={false}
            axisLine={false}
            minTickGap={28}
            tickFormatter={(value: string) => value.slice(5)}
          />
          <YAxis tickLine={false} axisLine={false} width={44} allowDecimals={false} />

          {/* Crosshair + tooltip: an SVG chart is interactive by default. */}
          <Tooltip
            content={<Tip suffix="leaks" />}
            cursor={{ stroke: "var(--border-2)", strokeWidth: 1 }}
          />

          <Area
            type="monotone"
            dataKey="total"
            stroke="var(--series-1)"
            strokeWidth={2}
            fill="url(#leakFill)"
            activeDot={{ r: 4, strokeWidth: 2, stroke: "var(--surface)" }}
            dot={false}
            isAnimationActive={false}
          />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}

/** Ranked magnitude by group. Horizontal so long group names stay readable. */
export function LeaksPerGroupChart({ data }: { data: { group: string; total: number }[] }) {
  return (
    <div className="chart-box">
      <ResponsiveContainer width="100%" height="100%">
        <BarChart
          data={data}
          layout="vertical"
          margin={{ top: 4, right: 40, bottom: 4, left: 8 }}
          barCategoryGap={6}
        >
          <CartesianGrid stroke="var(--grid)" horizontal={false} />
          <XAxis type="number" hide allowDecimals={false} />
          <YAxis
            type="category"
            dataKey="group"
            tickLine={false}
            axisLine={false}
            width={92}
          />
          <Tooltip
            content={<Tip suffix="leaks" />}
            cursor={{ fill: "var(--surface-hover)" }}
          />
          <Bar dataKey="total" radius={[0, 4, 4, 0]} isAnimationActive={false}>
            {data.map((row) => (
              <Cell key={row.group} fill="var(--series-1)" />
            ))}
            {/* Direct labels: the value is on the mark, so there's no axis to read across to. */}
            <LabelList
              dataKey="total"
              position="right"
              fill="var(--text-2)"
              fontSize={12}
            />
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}
