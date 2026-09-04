import {
  Boxes,
  CheckCircle2,
  CircleAlert,
  ExternalLink,
  LoaderCircle,
  Plus,
  Server,
  Trash2,
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
import type { AdminDeploymentView, MerchantView, TenantView } from "../types";
import { CopyButton, EmptyState, MetricCard, PageHeader, RefreshButton, SearchField } from "./ConsolePrimitives";
import { PhaseBadge } from "./PhaseBadge";
import { RuntimeBadge } from "./RuntimeBadge";

/**
* Deployment overview: full deployment across merchants and tenants, filterable by merchant/phase.
*
* The list reads the database snapshot of `list_all_deployments`,
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
  const [tenants, setTenants] = useState<TenantView[]>([]);
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
  const [creating, setCreating] = useState(false);
  const [mutating, setMutating] = useState("");
  const [formError, setFormError] = useState("");
  const [draftMerchant, setDraftMerchant] = useState("");
  const [draftUser, setDraftUser] = useState("");
  const [draftName, setDraftName] = useState("");
  const [draftImage, setDraftImage] = useState("");
  const [draftPort, setDraftPort] = useState("8080");
  const [draftHealthPath, setDraftHealthPath] = useState("/");
  const [draftExposure, setDraftExposure] = useState<"public" | "internal">("public");
  const [draftMemory, setDraftMemory] = useState("512Mi");

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
    Promise.all([api.listMerchants(), api.listTenants()])
      .then(([merchantResponse, tenantResponse]) => {
        const merchantRows = merchantResponse.merchants ?? [];
        const tenantRows = tenantResponse.tenants ?? [];
        setMerchants(merchantRows);
        setTenants(tenantRows);
        const firstTenant = tenantRows.find((item) => !item.disabledAt);
        if (firstTenant) {
          setDraftMerchant((value) => value || firstTenant.merchantId);
          setDraftUser((value) => value || firstTenant.userId);
        }
      })
      // The failure of the filter dropdown shouldn't make the entire page fail: the list itself is still useful.
      .catch(() => setMerchants([]));
  }, []);

  const targetTenants = tenants.filter(
    (item) => item.merchantId === draftMerchant && !item.disabledAt,
  );

  const submitDeployment = async (event: React.FormEvent) => {
    event.preventDefault();
    setFormError("");
    const port = Number(draftPort);
    if (!draftMerchant || !draftUser || !draftName.trim() || !draftImage.trim()) {
      setFormError(t("Merchant, tenant, application name, and image are required."));
      return;
    }
    if (!Number.isInteger(port) || port < 1 || port > 65535) {
      setFormError(t("Container port must be an integer from 1 to 65535."));
      return;
    }
    setMutating("__create__");
    try {
      await api.createDeployment({
        merchantId: draftMerchant,
        userId: draftUser,
        name: draftName.trim(),
        image: draftImage.trim(),
        port,
        healthPath: draftHealthPath.trim() || "/",
        exposure: draftExposure,
        memoryLimit: draftMemory.trim() || "512Mi",
      });
      setCreating(false);
      setDraftName("");
      setDraftImage("");
      await load(true);
    } catch (cause) {
      setFormError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setMutating("");
    }
  };

  const deleteDeployment = async (deployment: AdminDeploymentView) => {
    const identity = `${deployment.merchantId}/${deployment.userId}/${deployment.serviceName}`;
    if (!window.confirm(t("Delete application {identity}? Its workload and public route will be reclaimed.", { identity }))) return;
    setMutating(identity);
    try {
      await api.deleteDeployment(
        deployment.merchantId,
        deployment.userId,
        deployment.serviceName,
      );
      await load(true);
    } catch (cause) {
      onError(cause);
    } finally {
      setMutating("");
    }
  };

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
        actions={<div className="page-actions">
          <RefreshButton refreshing={loading || refreshing} onRefresh={() => void load(true)} />
          <button type="button" className="button button-primary" onClick={() => { setCreating((value) => !value); setFormError(""); }}>
            <Plus size={15} />{t("Deploy application")}
          </button>
        </div>}
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
        {creating ? (
          <form className="form-panel deployment-form" onSubmit={(event) => void submitDeployment(event)}>
            <label className="field"><span>{t("Merchant")}</span><select value={draftMerchant} onChange={(event) => {
              const next = event.target.value;
              setDraftMerchant(next);
              setDraftUser(tenants.find((item) => item.merchantId === next && !item.disabledAt)?.userId ?? "");
            }}><option value="">{t("Please select")}</option>{merchants.filter((item) => !item.disabledAt).map((item) => <option key={item.merchantId} value={item.merchantId}>{item.merchantId}</option>)}</select></label>
            <label className="field"><span>{t("Tenant ID")}</span><select value={draftUser} onChange={(event) => setDraftUser(event.target.value)}><option value="">{t("Please select")}</option>{targetTenants.map((item) => <option key={`${item.merchantId}/${item.userId}`} value={item.userId}>{item.userId}</option>)}</select></label>
            <label className="field"><span>{t("Application name")}</span><input value={draftName} placeholder="hello-site" onChange={(event) => setDraftName(event.target.value)} /></label>
            <label className="field deployment-image-field"><span>{t("Container image")}</span><input value={draftImage} placeholder="registry.example/app@sha256:..." onChange={(event) => setDraftImage(event.target.value)} /></label>
            <label className="field field-narrow"><span>{t("Container port")}</span><input type="number" min={1} max={65535} value={draftPort} onChange={(event) => setDraftPort(event.target.value)} /></label>
            <label className="field field-narrow"><span>{t("Health path")}</span><input value={draftHealthPath} onChange={(event) => setDraftHealthPath(event.target.value)} /></label>
            <label className="field field-narrow"><span>{t("Exposure")}</span><select value={draftExposure} onChange={(event) => setDraftExposure(event.target.value as "public" | "internal")}><option value="public">{t("Public")}</option><option value="internal">{t("Internal")}</option></select></label>
            <label className="field field-narrow"><span>{t("Memory limit")}</span><input value={draftMemory} onChange={(event) => setDraftMemory(event.target.value)} /></label>
            <div className="form-actions"><button className="button button-primary" type="submit" disabled={mutating === "__create__"}>{mutating === "__create__" ? <LoaderCircle className="spin" size={16} /> : null}{t("Deploy or update")}</button><button className="button" type="button" onClick={() => setCreating(false)}>{t("Cancel")}</button></div>
            {formError ? <p className="form-error" role="alert">{formError}</p> : null}
            <p className="form-note">{t("Use an existing container image. Submitting the same merchant, tenant, and application name updates that application.")}</p>
          </form>
        ) : null}
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
            description={query || merchantFilter || phase || runtime ? t("Adjust search term, merchant, phase or flex status filters.") : t("Deploy an existing container image here, or submit through CLI, MCP, or HTTP API.")}
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
                        <span className={`badge ${deployment.exposure === "internal" ? "badge-neutral" : "badge-ok"}`}>
                          {deployment.exposure === "internal" ? t("Internal") : t("Public")}
                        </span>
                      </div>
                      <p>{deployment.message || t("Sites have not reported detailed status yet")}</p>
                      <span className="deploy-updated">
                        {relativeTime(deployment.updatedAt, localeTag, t)}
                        {deployment.verification ? ` · ${t(deployment.verification.ok ? "control-plane verification passed" : "control-plane verification failed")}` : ` · ${t("No verification evidence returned")}`}
                      </span>
                    </div>
                    <div className="deploy-card-actions">{href ? (
                      <a className="button button-small" href={href} target="_blank" rel="noopener noreferrer">
                        <ExternalLink size={14} />
                        {deployment.runtimeState === "Dormant" ? t("Open (cold start)") : t("Open")}
                      </a>
                    ) : null}<button type="button" className="button button-small button-danger" disabled={mutating === `${deployment.merchantId}/${deployment.userId}/${deployment.serviceName}` || deployment.phase === "Deleting"} onClick={() => void deleteDeployment(deployment)}><Trash2 size={14} />{t("Delete")}</button></div>
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
                          ) : deployment.exposure === "internal" ? t("Internal only — no public URL") : t("Waiting for a public URL")}
                        </dd>
                      </div>
                      {deployment.siteVersion ? <div><dt>{t("Site version")}</dt><dd className="mono">v{deployment.siteVersion}</dd></div> : null}
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
