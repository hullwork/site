import { Activity, Boxes, Cpu, Gauge, LoaderCircle, MemoryStick, Server } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { api, SitesApiError } from "../api";
import { useI18n } from "../i18n";
import type {
  AdminDeploymentView,
  GrafanaCapability,
  MetricsRange,
  MonitoringResponse,
} from "../types";
import { EmptyState, MetricCard, PageHeader, RefreshButton } from "./ConsolePrimitives";
import GrafanaPanels from "./GrafanaPanels";
import MetricChart, { formatMetric } from "./MetricChart";

const RANGES: MetricsRange[] = ["1h", "6h", "24h"];

export default function MonitoringView({ onError }: { onError: (cause: unknown) => void }) {
  const { localeTag, t } = useI18n();
  const [scope, setScope] = useState<"cluster" | "application">("cluster");
  const [range, setRange] = useState<MetricsRange>("1h");
  const [deployments, setDeployments] = useState<AdminDeploymentView[]>([]);
  const [selected, setSelected] = useState("");
  const [data, setData] = useState<MonitoringResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  // Panels come from the admin health call, which is already admin-only. A
  // failure here must not disturb the charts: the panels are an addition to
  // this page, not a dependency of it.
  const [grafana, setGrafana] = useState<GrafanaCapability | null>(null);

  const selectedDeployment = useMemo(() => deployments.find((item) =>
    `${item.merchantId}/${item.userId}/${item.serviceName}` === selected), [deployments, selected]);

  useEffect(() => {
    api.health()
      .then((response) => setGrafana(response.grafana?.enabled ? response.grafana : null))
      .catch(() => setGrafana(null));
  }, []);

  useEffect(() => {
    api.listDeployments({ limit: 200 }).then((response) => {
      const rows = response.deployments ?? [];
      setDeployments(rows);
      const preferred = rows.find((item) => item.phase === "Running" && item.ready) ?? rows[0];
      if (preferred) setSelected(`${preferred.merchantId}/${preferred.userId}/${preferred.serviceName}`);
    }).catch(onError);
  }, [onError]);

  const load = useCallback(async () => {
    if (scope === "application" && !selectedDeployment) { setLoading(false); return; }
    setLoading(true);
    try {
      const response = scope === "cluster"
        ? await api.getClusterMetrics(range)
        : await api.getApplicationMetrics({
          merchantId: selectedDeployment!.merchantId,
          userId: selectedDeployment!.userId,
          serviceName: selectedDeployment!.serviceName,
        }, range);
      setData(response);
      setError("");
    } catch (cause) {
      if (cause instanceof SitesApiError && (cause.status === 401 || cause.status === 403)) onError(cause);
      else setError(cause instanceof Error ? cause.message : String(cause));
    } finally { setLoading(false); }
  }, [onError, range, scope, selectedDeployment]);

  useEffect(() => { void load(); }, [load]);

  const summary = data?.summary ?? {};
  return <div className="page">
    <PageHeader eyebrow={t("OBSERVABILITY")} title={t("Metrics monitoring")} description={t("Inspect resource and traffic trends collected from the Sites cluster. Switch between the cluster overview and individual applications; data is retained for 24 hours.")} actions={<RefreshButton refreshing={loading} onRefresh={() => void load()} />} meta={<span>{data?.source.sampledAt ? t("Sampled at {time}", { time: new Date(data.source.sampledAt).toLocaleTimeString(localeTag) }) : t("Waiting for first sample")}</span>} />

    <section className="monitor-controls" aria-label={t("Monitoring scope and time")}>
      <div className="segmented-control" aria-label={t("Monitoring scope")}>
        <button className={scope === "cluster" ? "is-active" : ""} aria-pressed={scope === "cluster"} onClick={() => setScope("cluster")} type="button"><Server size={15} />{t("Cluster")}</button>
        <button className={scope === "application" ? "is-active" : ""} aria-pressed={scope === "application"} onClick={() => setScope("application")} type="button"><Boxes size={15} />{t("Single application")}</button>
      </div>
      {scope === "application" ? <label className="compact-field monitor-app-select"><span>{t("Application")}</span><select value={selected} onChange={(event) => setSelected(event.target.value)}><option value="">{t("Select app")}</option>{deployments.map((item) => { const key = `${item.merchantId}/${item.userId}/${item.serviceName}`; return <option key={key} value={key}>{key} · {item.phase}</option>; })}</select></label> : null}
      <div className="segmented-control monitor-range" aria-label={t("Time range")}>{RANGES.map((item) => <button type="button" key={item} className={range === item ? "is-active" : ""} aria-pressed={range === item} onClick={() => setRange(item)}>{item}</button>)}</div>
    </section>

    {error ? <div className="banner banner-bad" role="alert"><Activity size={18} /><div><strong>{t("Metrics refresh failed")}</strong><p>{t("{error}. Charts retain the last successful data.", { error })}</p></div></div> : null}
    {data && !data.source.available ? <div className="banner banner-warn" role="status"><Activity size={18} /><div><strong>{t("The metric backend is temporarily unavailable")}</strong><p>{t("Prometheus is not ready or accessible, and the console does not replace real samples with imputed values.")}</p></div></div> : null}

    {loading && !data ? <div className="state page-loading"><LoaderCircle className="spin" size={22} /><strong>{t("Reading metrics")}</strong></div> : scope === "application" && !selectedDeployment ? <EmptyState icon={<Boxes size={22} />} title={t("There are no apps to monitor yet")} description={t("Once you create your application, you can view its resources, requests, error rates, and latency trends.")} /> : <>
      <section className="metric-grid metric-grid-four" aria-label={t("Current metric")}>
        <MetricCard label={t("CPU")} value={formatMetric(summary.cpu, "cores", t)} hint={scope === "cluster" ? `${t("Capacity")} ${formatMetric(summary.cpuCapacity, "cores", t)}` : selectedDeployment?.serviceName ?? t("Application")} icon={<Cpu size={19} />} />
        <MetricCard label={t("Memory")} value={formatMetric(summary.memory, "bytes", t)} hint={scope === "cluster" ? `${t("Capacity")} ${formatMetric(summary.memoryCapacity, "bytes", t)}` : t("Working set")} icon={<MemoryStick size={19} />} />
        <MetricCard label={t("Request rate")} value={formatMetric(summary.requests, "req/s", t)} hint={t("Envoy has completed the request")} icon={<Activity size={19} />} />
        <MetricCard label={t("P95 response")} value={formatMetric(summary.latencyP95, "ms", t)} hint={`${t("Error rate")} ${formatMetric(summary.errors, "percent", t)}`} icon={<Gauge size={19} />} />
      </section>
      <section className="monitor-grid" aria-label={t("Metric trend")}>{data?.series.filter((series) => !series.id.endsWith("Capacity")).map((series) => <MetricChart key={series.id} series={series} />)}</section>
    </>}

    {grafana ? <GrafanaPanels grafana={grafana} /> : null}
  </div>;
}
