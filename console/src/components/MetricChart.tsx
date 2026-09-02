import type { MetricSeries } from "../types";
import { useI18n, type TranslationKey } from "../i18n";
import type { Translator } from "../format";

const METRIC_LABEL_KEYS: Record<string, TranslationKey> = {
  "CPU usage": "CPU usage",
  "memory usage": "Memory usage",
  "Request rate": "Request rate",
  "4xx / 5xx error rate": "4xx / 5xx error rate",
  "P95 response time": "P95 response time",
};

export function formatMetric(
  value: number | null | undefined,
  unit: string,
  t: Translator,
): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return t("No sample yet");
  if (unit === "bytes") {
    const gib = value / 1024 ** 3;
    return gib >= 0.1 ? `${gib.toFixed(2)} GiB` : `${(value / 1024 ** 2).toFixed(1)} MiB`;
  }
  if (unit === "percent") return `${value.toFixed(2)}%`;
  if (unit === "ms") return `${value.toFixed(0)} ms`;
  if (unit === "req/s") return `${value.toFixed(2)} req/s`;
  return `${value.toFixed(3)} cores`;
}

export default function MetricChart({ series }: { series: MetricSeries }) {
  const { localeTag, t } = useI18n();
  const labelKey = METRIC_LABEL_KEYS[series.label];
  const label = labelKey ? t(labelKey) : series.label;
  const values = series.points.map((point) => point.value);
  const max = Math.max(...values, 0.000001);
  const min = Math.min(...values, 0);
  const width = 640;
  const height = 180;
  const path = series.points.map((point, index) => {
    const x = series.points.length < 2 ? width / 2 : index * width / (series.points.length - 1);
    const y = height - ((point.value - min) / Math.max(max - min, 0.000001)) * (height - 16) - 8;
    return `${index ? "L" : "M"}${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(" ");
  const current = series.points.at(-1)?.value;
  const description = t("{label}: current {value}, {count} samples.", {
    label,
    value: formatMetric(current, series.unit, t),
    count: series.points.length,
  });

  return (
    <article className="monitor-chart-card">
      <header><div><span>{label}</span><strong>{formatMetric(current, series.unit, t)}</strong></div><small>{t("{count} samples", { count: series.points.length })}</small></header>
      {series.points.length ? (
        <svg className="monitor-chart" viewBox={`0 0 ${width} ${height}`} role="img" aria-label={description} preserveAspectRatio="none">
          <line x1="0" y1={height - 1} x2={width} y2={height - 1} className="chart-grid-line" />
          <path d={path} className="chart-line" vectorEffect="non-scaling-stroke" />
        </svg>
      ) : <div className="chart-empty">{t("There are no samples yet for the selected time period")}</div>}
      <details className="chart-data">
        <summary>{t("View datasheet")}</summary>
        <div className="table-scroll"><table><thead><tr><th>{t("Time")}</th><th>{t("Value")}</th></tr></thead><tbody>
          {series.points.map((point) => <tr key={point.timestamp}><td>{new Date(point.timestamp * 1000).toLocaleString(localeTag)}</td><td>{formatMetric(point.value, series.unit, t)}</td></tr>)}
        </tbody></table></div>
      </details>
    </article>
  );
}
