import {
  Activity,
  Boxes,
  CheckCircle2,
  CircleAlert,
  Database,
  Hammer,
  Image,
  LoaderCircle,
  Server,
  Store,
  Users,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import type { ConsoleTab } from "../App";
import { api } from "../api";
import {
  formatSeconds,
  IDLE_POLL_MS,
  relativeTime,
  runtimeLabel,
  SNAPSHOT_STALE_SECONDS,
} from "../format";
import { useI18n } from "../i18n";
import type {
  AdminBuildView,
  AdminDeploymentView,
  AdminHealthResponse,
  HealthProbe,
  ImageRepositoryView,
  MerchantView,
  TenantView,
} from "../types";
import { MetricCard, PageHeader, RefreshButton } from "./ConsolePrimitives";
import { PhaseBadge } from "./PhaseBadge";

function ProbeCard({
  icon,
  label,
  probe,
  detail,
}: {
  icon: React.ReactNode;
  label: string;
  probe: HealthProbe | undefined;
  detail: string;
}) {
  const { t } = useI18n();
  const state = probe === undefined ? "unknown" : probe.reachable ? "ok" : "bad";
  return (
    <article className={`probe probe-${state}`}>
      <span className="probe-icon" aria-hidden="true">{icon}</span>
      <div className="probe-body">
        <div className="probe-heading">
          <span className="probe-label">{label}</span>
          <strong className="probe-state">
            {state === "ok" ? <CheckCircle2 size={15} /> : state === "bad" ? <CircleAlert size={15} /> : null}
            {state === "ok" ? t("Normal") : state === "bad" ? t("Abnormal") : t("Not reported")}
          </strong>
        </div>
        <span className="probe-detail">{state === "bad" ? probe?.error || t("The control plane does not give a reason") : detail}</span>
      </div>
    </article>
  );
}

export default function OverviewView({
  onError,
  onNavigate,
}: {
  onError: (error: unknown) => void;
  onNavigate: (tab: ConsoleTab) => void;
}) {
  const { localeTag, t } = useI18n();
  const [health, setHealth] = useState<AdminHealthResponse | null>(null);
  const [merchants, setMerchants] = useState<MerchantView[]>([]);
  const [tenants, setTenants] = useState<TenantView[]>([]);
  const [deployments, setDeployments] = useState<AdminDeploymentView[]>([]);
  const [builds, setBuilds] = useState<AdminBuildView[]>([]);
  const [repositories, setRepositories] = useState<ImageRepositoryView[]>([]);
  const [snapshotAge, setSnapshotAge] = useState<number | null | undefined>();
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);

  const load = useCallback(async (silent = false) => {
    if (silent) setRefreshing(true);
    const results = await Promise.allSettled([
      api.health(),
      api.listMerchants(),
      api.listTenants(),
      api.listDeployments({ limit: 200 }),
      api.listBuilds(),
      api.listImages(),
    ]);
    const [healthResult, merchantResult, tenantResult, deploymentResult, buildResult, imageResult] = results;
    if (healthResult.status === "fulfilled") setHealth(healthResult.value);
    if (merchantResult.status === "fulfilled") setMerchants(merchantResult.value.merchants ?? []);
    if (tenantResult.status === "fulfilled") setTenants(tenantResult.value.tenants ?? []);
    if (deploymentResult.status === "fulfilled") {
      setDeployments(deploymentResult.value.deployments ?? []);
      setSnapshotAge(deploymentResult.value.snapshotAgeSeconds);
    }
    if (buildResult.status === "fulfilled") setBuilds(buildResult.value.builds ?? []);
    if (imageResult.status === "fulfilled") setRepositories(imageResult.value.repositories ?? []);
    const failed = results.find((result) => result.status === "rejected");
    if (failed?.status === "rejected") onError(failed.reason);
    if (results.some((result) => result.status === "fulfilled")) setLastUpdated(new Date());
    setLoading(false);
    setRefreshing(false);
  }, [onError]);

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => void load(true), IDLE_POLL_MS);
    return () => window.clearInterval(timer);
  }, [load]);

  const summary = useMemo(() => ({
    activeMerchants: merchants.filter((item) => !item.disabledAt).length,
    disabledTenants: tenants.filter((item) => item.disabledAt).length,
    running: deployments.filter((item) => item.runtimeState === "Active").length,
    dormant: deployments.filter((item) => item.runtimeState === "Dormant").length,
    failed: deployments.filter((item) => item.phase === "Failed").length,
    activeBuilds: builds.filter((item) => ["Pending", "Building", "Deploying"].includes(item.phase)).length,
  }), [builds, deployments, merchants, tenants]);

  const recentDeployments = useMemo(() => [...deployments]
    .sort((left, right) => Date.parse(right.updatedAt || "") - Date.parse(left.updatedAt || ""))
    .slice(0, 5), [deployments]);

  const probes = [health?.database, health?.operator, health?.registry, health?.kubernetes];
  const healthyCount = probes.filter((probe) => probe?.reachable).length;
  const unhealthyCount = probes.filter((probe) => probe && !probe.reachable).length;
  const snapshotStale = snapshotAge !== undefined
    && (snapshotAge === null || snapshotAge > SNAPSHOT_STALE_SECONDS);
  const credentialRiskCount = merchants.filter((merchant) => {
    if (!merchant.keyExpiresAt || merchant.disabledAt) return false;
    const expires = Date.parse(merchant.keyExpiresAt);
    return Number.isFinite(expires) && expires <= Date.now() + 7 * 24 * 60 * 60 * 1000;
  }).length;

  if (loading) {
    return <div className="state page-loading" aria-live="polite"><LoaderCircle className="spin" size={22} /><strong>{t("Summarizing control plane status")}</strong><span>{t("Health, Tenants, Deployments, Builds and Registry")}</span></div>;
  }

  return (
    <div className="page">
      <PageHeader
        eyebrow={t("OPERATIONS OVERVIEW")}
        title={t("Platform overview")}
        description={t("Prioritize what needs work from control plane dependencies, tenant quotas, and workload status.")}
        meta={<span aria-live="polite">{lastUpdated ? t("Updated {time}", { time: lastUpdated.toLocaleTimeString(localeTag, { hour: "2-digit", minute: "2-digit", second: "2-digit" }) }) : t("Not updated yet")}</span>}
        actions={<RefreshButton refreshing={refreshing} onRefresh={() => void load(true)} />}
      />

      <section className="metric-grid" aria-label={t("Platform key metrics")}>
        <MetricCard label={t("Control plane dependencies")} value={`${healthyCount}/4`} hint={unhealthyCount ? (unhealthyCount === 1 ? t("1 probe needs attention") : t("{count} probes need attention", { count: unhealthyCount })) : t("All probes are normal")} icon={<Activity size={19} />} tone={unhealthyCount ? "bad" : "good"} />
        <MetricCard label={t("Enabled merchants")} value={summary.activeMerchants} hint={t("{count} total merchants", { count: merchants.length })} icon={<Store size={19} />} onClick={() => onNavigate("merchants")} />
        <MetricCard label={t("Tenants")} value={tenants.length} hint={summary.disabledTenants ? t("{count} disabled", { count: summary.disabledTenants }) : t("All credentials enabled")} icon={<Users size={19} />} onClick={() => onNavigate("tenants")} />
        <MetricCard label={t("Active workloads")} value={summary.running} hint={t("{count} dormant · {records} snapshot records", { count: summary.dormant, records: deployments.length })} icon={<Boxes size={19} />} tone={summary.failed ? "warn" : "good"} onClick={() => onNavigate("deployments")} />
        <MetricCard label={t("Failed deployments")} value={summary.failed} hint={summary.failed ? t("Check the status reason") : t("No recorded failures")} icon={<CircleAlert size={19} />} tone={summary.failed ? "bad" : "good"} onClick={() => onNavigate("deployments")} />
        <MetricCard label={t("Active builds")} value={summary.activeBuilds} hint={t("{count} local repositories", { count: repositories.length })} icon={<Hammer size={19} />} tone={summary.activeBuilds ? "warn" : "neutral"} onClick={() => onNavigate("builds")} />
      </section>

      {(unhealthyCount > 0 || summary.failed > 0 || snapshotStale || credentialRiskCount > 0) ? (
        <section className="attention-card" aria-labelledby="attention-title">
          <div>
            <span className="section-kicker">{t("Need attention")}</span>
            <h3 id="attention-title">{t("The control plane has actionable signals")}</h3>
          </div>
          <div className="attention-list">
            {unhealthyCount > 0 ? <button type="button" onClick={() => onNavigate("overview")}><CircleAlert size={17} /><span><strong>{unhealthyCount === 1 ? t("1 dependency probe failing") : t("{count} dependency probes failing", { count: unhealthyCount })}</strong><small>{t("See errors and recovery directions below")}</small></span></button> : null}
            {summary.failed > 0 ? <button type="button" onClick={() => onNavigate("deployments")}><Boxes size={17} /><span><strong>{summary.failed === 1 ? t("1 failed deployment") : t("{count} failed deployments", { count: summary.failed })}</strong><small>{t("Filter by failed phase and expand run details")}</small></span></button> : null}
            {snapshotStale ? <button type="button" onClick={() => onNavigate("deployments")}><Database size={17} /><span><strong>{t("Deployment snapshot is stale")}</strong><small>{snapshotAge === null ? t("The control plane has not completed its first sync") : t("No sync for {count} seconds", { count: Math.round(snapshotAge) })}</small></span></button> : null}
            {credentialRiskCount > 0 ? <button type="button" onClick={() => onNavigate("merchants")}><Store size={17} /><span><strong>{credentialRiskCount === 1 ? t("1 merchant key needs rotation") : t("{count} merchant keys need rotation", { count: credentialRiskCount })}</strong><small>{t("Expired or expiring within seven days")}</small></span></button> : null}
          </div>
        </section>
      ) : null}

      <div className="overview-columns">
        <section className="card">
          <div className="section-header"><div><span className="section-kicker">{t("CONTROL PLANE")}</span><h3>{t("Dependency health")}</h3></div><span className="card-count">{t("Auto-refreshes every {seconds} seconds", { seconds: IDLE_POLL_MS / 1000 })}</span></div>
          <div className="probe-grid">
            <ProbeCard icon={<Database size={18} />} label={t("Metadata database")} probe={health?.database} detail={`${health?.database?.backend ?? t("Backend not reported")} · ${t("snapshot")} ${formatSeconds(health?.database?.snapshotAgeSeconds, t)}`} />
            <ProbeCard icon={<Server size={18} />} label={t("Operator")} probe={health?.operator} detail={`${t("Last reconcile")} ${formatSeconds(health?.operator?.lastReconcileSeconds, t)}`} />
            <ProbeCard icon={<Image size={18} />} label={t("Workload registry")} probe={health?.registry} detail={health?.registry?.repositoryCount === undefined ? t("Not reported") : t("{count} repositories", { count: health.registry.repositoryCount })} />
            <ProbeCard icon={<Server size={18} />} label={t("Kubernetes API")} probe={health?.kubernetes} detail={health?.kubernetes?.version ?? t("Version not reported")} />
          </div>
        </section>

        <section className="card recent-card">
          <div className="section-header"><div><span className="section-kicker">{t("RECENT ACTIVITY")}</span><h3>{t("Recently deployed")}</h3></div><button type="button" className="text-button" onClick={() => onNavigate("deployments")}>{t("View all")}</button></div>
          {recentDeployments.length ? <div className="recent-list">{recentDeployments.map((item) => (
            <button type="button" key={item.name || `${item.merchantId}/${item.userId}/${item.serviceName}`} onClick={() => onNavigate("deployments")}>
              <span className="recent-main"><strong>{item.serviceName}</strong><small>{item.merchantId} / {item.userId} · {runtimeLabel(item.runtimeState, t)}</small></span>
              <PhaseBadge phase={item.phase} />
              <span className="recent-time">{relativeTime(item.updatedAt, localeTag, t)}</span>
            </button>
          ))}</div> : <p className="empty compact-empty">{t("There are no deployment records on the platform yet.")}</p>}
        </section>
      </div>
    </div>
  );
}
