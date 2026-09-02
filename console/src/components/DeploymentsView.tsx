import {
  Boxes,
  CheckCircle2,
  CircleAlert,
  ExternalLink,
  LoaderCircle,
  Server,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "../api";
import {
  ACTIVE_PHASES,
  ACTIVE_POLL_MS,
  formatAge,
  IDLE_POLL_MS,
  relativeTime,
  runtimeLabel,
  safeHttpUrl,
  shortDigest,
  SNAPSHOT_STALE_SECONDS,
  verificationLabel,
  phaseLabel,
} from "../format";
import { useI18n } from "../i18n";
import type { AdminDeploymentView, MerchantView } from "../types";
import { CopyButton, EmptyState, MetricCard, PageHeader, RefreshButton, SearchField } from "./ConsolePrimitives";
import { PhaseBadge } from "./PhaseBadge";
import { RuntimeBadge } from "./RuntimeBadge";

/**
* Deployment overview: full deployment across merchants and tenants, filterable by merchant/phase.
*
* Polling, stale alarms, phase copywriting and verification display all use the Work UI
* `web/src/components/DeploymentsView.tsx`——When the admin console and tenant UI view the same deployment
* The same conclusion must be given.
*
* A substantial difference from the tenant UI: what is read here is the database snapshot of `list_all_deployments`,
* **There is no entry back to Kubernetes** (there is no single detail endpoint in the admin scope in the contract).
* Therefore, "Run Details" only expands the existing fields in the list, and will not expand and return to the source like the tenant UI;
* When the snapshot is suspended, this page can only display stale data, and the banner must make it clear.
 */

const PHASE_OPTIONS = [
  "Pending",
  "Building",
  "Deploying",
  "Running",
  "Failed",
  "Deleting",
  "Deleted",
];

const RUNTIME_OPTIONS = ["Active", "Dormant", "Unknown"];

const LIST_LIMIT = 200;

export default function DeploymentsView({
  merchantFilter,
  onMerchantFilterChange,
  onError,
}: {
  merchantFilter: string;
  onMerchantFilterChange: (merchantId: string) => void;
  onError: (cause: unknown) => void;
}) {
  const { localeTag, t } = useI18n();
  const [deployments, setDeployments] = useState<AdminDeploymentView[]>([]);
  const [merchants, setMerchants] = useState<MerchantView[]>([]);
  const [phase, setPhase] = useState("");
  const [runtime, setRuntime] = useState("");
  const [snapshotAge, setSnapshotAge] = useState<number | null | undefined>(undefined);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [query, setQuery] = useState("");
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);
  // The last refresh failed: the card below is still the same card as before the failure, and must be marked, otherwise people who see it will not be able to tell the difference.
  // Which states still count.
  const [stale, setStale] = useState(false);

  const load = useCallback(async (silent = false) => {
    if (silent) setRefreshing(true);
    try {
      const response = await api.listDeployments({
        merchantId: merchantFilter || undefined,
        phase: phase || undefined,
        limit: LIST_LIMIT,
      });
      setDeployments(response.deployments ?? []);
      setSnapshotAge(response.snapshotAgeSeconds);
      setStale(false);
      setLastUpdated(new Date());
    } catch (cause) {
      setStale(true);
      onError(cause);
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, [merchantFilter, onError, phase]);

  useEffect(() => {
    api.listMerchants()
      .then((response) => setMerchants(response.merchants ?? []))
      // The failure of the filter dropdown shouldn't make the entire page fail: the list itself is still useful.
      .catch(() => setMerchants([]));
  }, []);

  useEffect(() => { void load(); }, [load]);

  const hasActive = useMemo(
    () => deployments.some((item) => ACTIVE_PHASES.has(item.phase)),
    [deployments],
  );

  useEffect(() => {
    const timer = window.setInterval(
      () => { void load(true); },
      hasActive ? ACTIVE_POLL_MS : IDLE_POLL_MS,
    );
    return () => window.clearInterval(timer);
  }, [hasActive, load]);

  const snapshotStale =
    snapshotAge !== undefined
    && (snapshotAge === null || snapshotAge > SNAPSHOT_STALE_SECONDS);
  const snapshotNote = snapshotAge
    ? t("The Sites control plane has not synced Kubernetes state for {age}. ", {
      age: formatAge(snapshotAge, t),
    })
    : t("The Sites control plane has not completed a Kubernetes synchronization.");

  const activeRuntimeCount = deployments.filter(
    (item) => item.runtimeState === "Active",
  ).length;
  const dormantCount = deployments.filter(
    (item) => item.runtimeState === "Dormant",
  ).length;
  const convergedCount = deployments.filter(
    (item) => item.phase === "Running" && item.ready,
  ).length;
  const failedCount = deployments.filter((item) => item.phase === "Failed").length;
  const activeCount = deployments.filter((item) => ACTIVE_PHASES.has(item.phase)).length;
  const verifiedCount = deployments.filter((item) => item.verification?.ok).length;
  const normalizedQuery = query.trim().toLowerCase();
  const visibleDeployments = deployments.filter((item) => (
    (!runtime || item.runtimeState === runtime)
  ) && (
    !normalizedQuery || [
    item.serviceName,
    item.name,
    item.merchantId,
    item.userId,
    item.image,
    item.message,
    ].some((value) => String(value || "").toLowerCase().includes(normalizedQuery))
  ));

  return (
    <div className="page">
      <PageHeader
        eyebrow={t("RUNTIME OPERATIONS")}
        title={t("Deployments")}
        description={t("View cross-merchant deployment snapshots, control-plane verification evidence, and public access URLs. Status comes from database snapshots and is not presented as a live Kubernetes view.")}
        meta={<span aria-live="polite">{lastUpdated ? t("Updated {time}", { time: lastUpdated.toLocaleTimeString(localeTag) }) : t("Not updated yet")} · {t("snapshot")} {snapshotAge === undefined ? t("Not reported") : snapshotAge === null ? t("Never synced") : t("{count} seconds ago", { count: snapshotAge.toFixed(1) })}</span>}
        actions={<RefreshButton refreshing={loading || refreshing} onRefresh={() => void load(true)} />}
      />

      <section className="metric-grid" aria-label={t("Deployment summary")}>
        <MetricCard label={t("Active workloads")} value={activeRuntimeCount} hint={t("{count} lifecycle Running", { count: convergedCount })} icon={<CheckCircle2 size={19} />} tone="good" />
        <MetricCard label={t("Dormant sites")} value={dormantCount} hint={t("0 replicas; activator wakes them on access")} icon={<Server size={19} />} tone={dormantCount ? "neutral" : "good"} />
        <MetricCard label={t("Advancing")} value={activeCount} hint={t("Wait, build, deploy, or delete")} icon={<Server size={19} />} tone={activeCount ? "warn" : "neutral"} />
        <MetricCard label={t("Failed")} value={failedCount} hint={failedCount ? t("Check the status reason") : t("No recorded failures")} icon={<CircleAlert size={19} />} tone={failedCount ? "bad" : "good"} />
        <MetricCard label={t("Verification passed")} value={verifiedCount} hint={t("Control-plane HTTP probe evidence")} icon={<Boxes size={19} />} />
        <MetricCard label={t("Snapshot records")} value={deployments.length} hint={t("Lifecycle Running and ready: {count}", { count: convergedCount })} icon={<Boxes size={19} />} tone="neutral" />
      </section>

      <section className="workspace-card">
        <div className="workspace-toolbar deployment-toolbar">
          <SearchField value={query} onChange={setQuery} label={t("Search deployment")} placeholder={t("Search for services, tenants, images, or status reasons")} />
          <div className="workspace-filters">
            <label className="compact-field">
              <span>{t("Merchant")}</span>
              <select
                value={merchantFilter}
                onChange={(event) => onMerchantFilterChange(event.target.value)}
              >
                <option value="">{t("All merchants")}</option>
                {merchants.map((merchant) => (
                  <option key={merchant.merchantId} value={merchant.merchantId}>
                    {merchant.merchantId}
                  </option>
                ))}
              </select>
            </label>
            <label className="compact-field">
              <span>{t("Scaling state")}</span>
              <select value={runtime} onChange={(event) => setRuntime(event.target.value)}>
                <option value="">{t("All status")}</option>
                {RUNTIME_OPTIONS.map((option) => (
                  <option key={option} value={option}>{runtimeLabel(option, t)}</option>
                ))}
              </select>
            </label>
            <label className="compact-field">
              <span>{t("Phase")}</span>
              <select value={phase} onChange={(event) => setPhase(event.target.value)}>
                <option value="">{t("All phases")}</option>
                {PHASE_OPTIONS.map((option) => (
                  <option key={option} value={option}>{phaseLabel(option, t)}</option>
                ))}
              </select>
            </label>
            <span className="card-count">{t("Showing {visible} / {total}", { visible: visibleDeployments.length, total: deployments.length })}</span>
          </div>
        </div>

        {snapshotStale ? (
          <div className="banner banner-warn" role="status">
            <CircleAlert size={18} aria-hidden="true" />
            <div>
              <strong>{t("Status snapshot has stopped updating")}</strong>
              <p>
                {snapshotNote}{t("The phase and access URL below may not match reality. The admin console does not query Kubernetes directly; inspect the control-plane synchronization thread.")}
              </p>
            </div>
          </div>
        ) : null}

        {loading ? (
          <div className="state" aria-live="polite">
            <LoaderCircle className="spin" size={20} />
            <strong>{t("Reading deployment snapshot")}</strong>
          </div>
        ) : visibleDeployments.length === 0 ? (
          <EmptyState
            icon={<Boxes size={22} />}
            title={query || merchantFilter || phase || runtime ? t("No matching deployment") : t("Not yet deployed on the platform")}
            description={query || merchantFilter || phase || runtime ? t("Adjust search term, merchant, phase or flex status filters.") : t("Deployments submitted via CLI, MCP, or Work UI will appear here.")}
          />
        ) : (
          <div className={`deploy-list ${stale ? "is-stale" : ""}`}>
            {visibleDeployments.map((deployment) => {
              const href = deployment.phase === "Running" && deployment.ready
                ? safeHttpUrl(deployment.url)
                : null;
              return (
                <article className="deploy-card" key={deployment.name || `${deployment.merchantId}/${deployment.userId}/${deployment.serviceName}`}>
                  <div className="deploy-card-main">
                    <span className="deploy-resource-icon" aria-hidden="true"><Server size={20} /></span>
                    <div className="deploy-card-copy">
                      <div className="deploy-card-title">
                        <h3>{deployment.serviceName}</h3>
                        <PhaseBadge phase={deployment.phase} />
                        <RuntimeBadge
                          runtime={deployment.runtimeState}
                          replicas={deployment.observedReplicas}
                        />
                        <span className="owner-chip mono">
                          {deployment.merchantId} / {deployment.userId}
                        </span>
                      </div>
                      <p>{deployment.message || t("Sites have not reported detailed status yet")}</p>
                      <span className="deploy-updated">
                        {relativeTime(deployment.updatedAt, localeTag, t)}
                        {deployment.verification ? ` · ${t(deployment.verification.ok ? "control-plane verification passed" : "control-plane verification failed")}` : ` · ${t("No verification evidence returned")}`}
                      </span>
                    </div>
                    {href ? (
                      <a className="button button-small" href={href} target="_blank" rel="noopener noreferrer">
                        <ExternalLink size={14} />
                        {deployment.runtimeState === "Dormant" ? t("Open (cold start)") : t("Open")}
                      </a>
                    ) : null}
                  </div>
                  <details className="deploy-details">
                    <summary>{t("Run details")}</summary>
                    <dl>
                      <div><dt>{t("Resource ID")}</dt><dd className="mono copy-line"><span>{deployment.name || "—"}</span>{deployment.name ? <CopyButton value={deployment.name} label={t("Copy resource ID")} /> : null}</dd></div>
                      <div><dt>{t("Image")}</dt><dd className="mono copy-line"><span>{deployment.image || "—"}</span>{deployment.image ? <CopyButton value={deployment.image} label={t("Copy image reference")} /> : null}</dd></div>
                      <div>
                        <dt>{t("Port/Health Check")}</dt>
                        <dd>{deployment.port ?? "—"} · {deployment.healthPath || "—"}</dd>
                      </div>
                      <div><dt>{t("Revision")}</dt><dd className="mono">{deployment.revision || "—"}</dd></div>
                      <div>
                        <dt>{t("Scaling state")}</dt>
                        <dd>
                          {runtimeLabel(deployment.runtimeState, t)}
                          {deployment.scaleToZero === undefined ? "" : deployment.scaleToZero ? ` · ${t("scale-to-zero enabled")}` : ` · ${t("always-on replicas")}`}
                          {deployment.observedReplicas === undefined || deployment.observedReplicas === null ? "" : ` · ${t(deployment.observedReplicas === 1 ? "{count} replica" : "{count} replicas", { count: deployment.observedReplicas })}`}
                        </dd>
                      </div>
                      <div>
                        <dt>{t("Access URL")}</dt>
                        <dd>
                          {href ? (
                            <a href={href} target="_blank" rel="noopener noreferrer">{href}</a>
                          ) : deployment.url ? (
                            // A non-whitelisted scheme is downgraded to plain text. Do not make it clickable,
                            // but do not hide it: hiding it would suggest the control plane returned no URL.
                            <span className="mono">{t("{url} (not http/https; link blocked)", { url: deployment.url })}</span>
                          ) : "—"}
                        </dd>
                      </div>
                      <div><dt>{t("Control plane verification")}</dt><dd>{verificationLabel(deployment.verification, t)}</dd></div>
                      {deployment.verification?.bodySha256 ? (
                        <div>
                          <dt>{t("Response summary")}</dt>
                          <dd className="mono">{shortDigest(deployment.verification.bodySha256)}</dd>
                        </div>
                      ) : null}
                      {deployment.artifactSha256 ? (
                        <div>
                          <dt>{t("Submit summary")}</dt>
                          <dd className="mono">{shortDigest(deployment.artifactSha256)}</dd>
                        </div>
                      ) : null}
                      <div><dt>{t("Created")}</dt><dd>{relativeTime(deployment.createdAt, localeTag, t)}</dd></div>
                    </dl>
                  </details>
                </article>
              );
            })}
          </div>
        )}
      </section>
    </div>
  );
}
