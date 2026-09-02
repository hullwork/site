import {
  Boxes,
  CircleAlert,
  CircleCheck,
  Hammer,
  Image,
  Layers3,
  LoaderCircle,
  PackageSearch,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api";
import { ACTIVE_PHASES, ACTIVE_POLL_MS, IDLE_POLL_MS, shortDigest, type Translator } from "../format";
import { useI18n } from "../i18n";
import type { AdminBuildView, AdminDeploymentView, ImageRepositoryView, ImageTagView, MerchantView } from "../types";
import { CopyButton, EmptyState, MetricCard, PageHeader, RefreshButton, SearchField } from "./ConsolePrimitives";
import { PhaseBadge } from "./PhaseBadge";

type AssetTab = "builds" | "registry" | "references";

function preferredAssetTab(
  builds: AdminBuildView[],
  repositories: ImageRepositoryView[],
  deployments: AdminDeploymentView[],
): AssetTab {
  if (builds.length) return "builds";
  if (repositories.some((repository) => (repository.tags?.length ?? 0) > 0)) {
    return "registry";
  }
  if (deployments.some((deployment) => Boolean(deployment.image))) return "references";
  // An empty repository is still information that needs to be processed, taking precedence over a completely blank view of build tasks.
  if (repositories.length) return "registry";
  return "builds";
}

function repositoryFromReference(reference: string): string {
  const withoutDigest = reference.split("@", 1)[0] ?? reference;
  const slash = withoutDigest.lastIndexOf("/");
  const colon = withoutDigest.lastIndexOf(":");
  const withoutTag = colon > slash ? withoutDigest.slice(0, colon) : withoutDigest;
  const parts = withoutTag.split("/");
  const registry = parts[0] ?? "";
  if (parts.length > 1 && (registry.includes(".") || registry.includes(":") || registry === "localhost")) {
    return parts.slice(1).join("/");
  }
  return withoutTag;
}

function referenceOrigin(reference: string, local: boolean, t: Translator): string {
  if (local) return t("Workload Registry");
  if (reference.includes("@sha256:")) return t("Immutable external image");
  return t("External Registry");
}

export default function BuildsView({
  merchantFilter,
  onMerchantFilterChange,
  onError,
}: {
  merchantFilter: string;
  onMerchantFilterChange: (merchantId: string) => void;
  onError: (cause: unknown) => void;
}) {
  const { localeTag, t } = useI18n();
  const [builds, setBuilds] = useState<AdminBuildView[]>([]);
  const [repositories, setRepositories] = useState<ImageRepositoryView[]>([]);
  const [deployments, setDeployments] = useState<AdminDeploymentView[]>([]);
  const [registryError, setRegistryError] = useState<string | null>(null);
  const [digestsTruncated, setDigestsTruncated] = useState(false);
  const [merchants, setMerchants] = useState<MerchantView[]>([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [tab, setTab] = useState<AssetTab>("builds");
  const [query, setQuery] = useState("");
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);
  const initialTabResolved = useRef(false);

  const selectTab = useCallback((next: AssetTab) => {
    initialTabResolved.current = true;
    setTab(next);
  }, []);

  const load = useCallback(async (silent = false) => {
    if (silent) setRefreshing(true);
    try {
      const [buildList, imageList, deploymentList] = await Promise.all([
        api.listBuilds(merchantFilter || undefined),
        api.listImages(),
        api.listDeployments({ merchantId: merchantFilter || undefined, limit: 200 }),
      ]);
      setBuilds(buildList.builds ?? []);
      setRepositories(imageList.repositories ?? []);
      setDeployments(deploymentList.deployments ?? []);
      if (!initialTabResolved.current) {
        setTab(preferredAssetTab(
          buildList.builds ?? [],
          imageList.repositories ?? [],
          deploymentList.deployments ?? [],
        ));
        initialTabResolved.current = true;
      }
      setDigestsTruncated(Boolean(imageList.digestsTruncated));
      setRegistryError(imageList.registry?.reachable === false
        ? imageList.registry.error ?? ""
        : null);
      setLastUpdated(new Date());
    } catch (cause) {
      onError(cause);
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, [merchantFilter, onError]);

  useEffect(() => {
    api.listMerchants().then((response) => setMerchants(response.merchants ?? [])).catch(() => setMerchants([]));
  }, []);
  useEffect(() => { void load(); }, [load]);

  const hasActive = useMemo(() => builds.some((item) => ACTIVE_PHASES.has(item.phase)), [builds]);
  useEffect(() => {
    const timer = window.setInterval(() => void load(true), hasActive ? ACTIVE_POLL_MS : IDLE_POLL_MS);
    return () => window.clearInterval(timer);
  }, [hasActive, load]);

  const normalizedQuery = query.trim().toLowerCase();
  const visibleBuilds = builds.filter((item) => !normalizedQuery || [item.serviceName, item.name, item.userId, item.merchantId, item.message, item.jobName, item.image]
    .some((value) => String(value || "").toLowerCase().includes(normalizedQuery)));
  const registryAssets: Array<{ repository: ImageRepositoryView; tag: ImageTagView | null }> = repositories.flatMap((repository): Array<{ repository: ImageRepositoryView; tag: ImageTagView | null }> => {
    const tags = repository.tags ?? [];
    return tags.length ? tags.map((tag) => ({ repository, tag })) : [{ repository, tag: null }];
  }).filter(({ repository, tag }) => !normalizedQuery || `${repository.name} ${tag?.tag || ""} ${tag?.digest || ""}`.toLowerCase().includes(normalizedQuery));

  const deploymentReferences = useMemo(() => {
    const grouped = new Map<string, AdminDeploymentView[]>();
    for (const deployment of deployments) {
      if (!deployment.image) continue;
      grouped.set(deployment.image, [...(grouped.get(deployment.image) ?? []), deployment]);
    }
    return [...grouped.entries()].map(([reference, rows]) => ({ reference, rows }));
  }, [deployments]);
  const visibleReferences = deploymentReferences.filter(({ reference, rows }) => !normalizedQuery
    || reference.toLowerCase().includes(normalizedQuery)
    || rows.some((row) => `${row.serviceName} ${row.userId} ${row.merchantId}`.toLowerCase().includes(normalizedQuery)));

  const activeBuildCount = builds.filter((item) => ACTIVE_PHASES.has(item.phase)).length;
  const failedBuildCount = builds.filter((item) => item.phase === "Failed").length;
  const tagCount = repositories.reduce((total, repository) => total + (repository.tags?.length ?? 0), 0);
  const registryRepositories = new Set(repositories.map((item) => item.name));
  const localReferenceCount = deploymentReferences.filter(({ reference }) => registryRepositories.has(repositoryFromReference(reference))).length;

  return (
    <div className="page">
      <PageHeader
        eyebrow={t("BUILD & ARTIFACTS")}
        title={t("Builds and images")}
        description={t("Relate SiteBuilds, build artifacts, and deployment references so Registry manifests are no longer mistaken for platform-wide images.")}
        meta={<span aria-live="polite">{lastUpdated ? t("Updated {time}", { time: lastUpdated.toLocaleTimeString(localeTag) }) : t("Not updated yet")} · {t(hasActive ? "5-second polling" : "20-second polling")}</span>}
        actions={<RefreshButton refreshing={loading || refreshing} onRefresh={() => void load(true)} />}
      />

      <section className="metric-grid metric-grid-four" aria-label={t("Build and Image Summary")}>
        <MetricCard label={t("Active builds")} value={activeBuildCount} hint={t("{count} current SiteBuilds", { count: builds.length })} icon={<Hammer size={19} />} tone={activeBuildCount ? "warn" : "neutral"} />
        <MetricCard label={t("Failed builds")} value={failedBuildCount} hint={failedBuildCount ? t("Expand a build to see the reason") : t("No failed builds")} icon={<CircleAlert size={19} />} tone={failedBuildCount ? "bad" : "good"} />
        <MetricCard label={t("Registry artifacts")} value={tagCount} hint={t("{count} repositories", { count: repositories.length })} icon={<Image size={19} />} tone={registryError !== null ? "bad" : "neutral"} />
        <MetricCard label={t("Deployment image references")} value={deploymentReferences.length} hint={t("{count} matched to local repositories", { count: localReferenceCount })} icon={<Layers3 size={19} />} />
      </section>

      <section className="workspace-card">
        <div className="workspace-toolbar">
          <div className="segmented-control" role="tablist" aria-label={t("Build and image views")}>
            <button type="button" role="tab" aria-selected={tab === "builds"} className={tab === "builds" ? "is-active" : ""} onClick={() => selectTab("builds")}><Hammer size={16} />{t("Build tasks")} <span>{builds.length}</span></button>
            <button type="button" role="tab" aria-selected={tab === "registry"} className={tab === "registry" ? "is-active" : ""} onClick={() => selectTab("registry")}><Image size={16} />{t("Registry")} <span>{tagCount}</span></button>
            <button type="button" role="tab" aria-selected={tab === "references"} className={tab === "references" ? "is-active" : ""} onClick={() => selectTab("references")}><Boxes size={16} />{t("Deployment references")} <span>{deploymentReferences.length}</span></button>
          </div>
          <div className="workspace-filters">
            <SearchField value={query} onChange={setQuery} label={t("Search builds and images")} placeholder={t("Search services, repositories, tags or digests")} />
            <label className="compact-field"><span>{t("Merchant")}</span><select value={merchantFilter} onChange={(event) => onMerchantFilterChange(event.target.value)}><option value="">{t("All merchants")}</option>{merchants.map((merchant) => <option key={merchant.merchantId} value={merchant.merchantId}>{merchant.displayName || merchant.merchantId}</option>)}</select></label>
          </div>
        </div>

        {registryError !== null && tab === "registry" ? <div className="banner banner-bad" role="alert"><CircleAlert size={18} /><div><strong>{t("Workload Registry is unreachable")}</strong><p>{registryError || t("Registry is unreachable, the control plane does not give a reason")}</p></div></div> : null}
        {digestsTruncated && tab === "registry" ? <div className="banner banner-warn" role="status"><CircleAlert size={18} /><div><strong>{t("Digest only shows partial results")}</strong><p>{t("Query the number of times or time budget for triggering the Registry; an empty Digest does not mean that the image is damaged.")}</p></div></div> : null}

        {loading ? <div className="state"><LoaderCircle className="spin" size={20} /><strong>{t("Reading builds and images")}</strong></div> : null}

        {!loading && tab === "builds" ? (
          visibleBuilds.length ? <div className="build-list">{visibleBuilds.map((build) => (
            <article className="build-card" key={build.name || `${build.merchantId}/${build.userId}/${build.serviceName}`}>
              <div className="build-card-icon" aria-hidden="true"><Hammer size={18} /></div>
              <div className="build-card-body">
                <div className="asset-title"><div><h3>{build.serviceName || t("Unnamed service")}</h3><span className="mono">{build.merchantId} / {build.userId}</span></div><PhaseBadge phase={build.phase} /></div>
                <p className={build.phase === "Failed" ? "error-copy" : ""}>{build.message || t("The control plane has not reported build status yet")}</p>
                <dl className="asset-meta">
                  <div><dt>{t("Build ID")}</dt><dd className="mono">{build.name || "—"}</dd></div>
                  <div><dt>{t("BuildKit Job")}</dt><dd className="mono">{build.jobName || t("Not created yet")}</dd></div>
                  <div><dt>{t("Output image")}</dt><dd className="mono copy-line"><span>{build.image || t("Not produced yet")}</span>{build.image ? <CopyButton value={build.image} label={t("Copy image reference")} /> : null}</dd></div>
                  <div><dt>{t("Digest")}</dt><dd className="mono copy-line"><span title={build.imageDigest || undefined}>{shortDigest(build.imageDigest)}</span>{build.imageDigest ? <CopyButton value={build.imageDigest} label={t("Copy digest")} /> : null}</dd></div>
                </dl>
              </div>
            </article>
          ))}</div> : <EmptyState icon={<Hammer size={22} />} title={query ? t("No matching builds") : t("No SiteBuilds yet")} description={query ? t("Adjust the search terms or merchant filter.") : t("This view shows current SiteBuilds, not build history; artifacts and running images are available on other tabs.")} action={<button type="button" className="button button-small" onClick={() => selectTab(deploymentReferences.length ? "references" : "registry")}>{deploymentReferences.length ? t("View deployment references") : t("View Registry artifacts")}</button>} />
        ) : null}

        {!loading && tab === "registry" ? (
          registryAssets.length ? <div className="asset-grid">{registryAssets.map(({ repository, tag }) => {
            const reference = tag ? `${repository.name}:${tag.tag}` : repository.name;
            const usage = deployments.filter((deployment) => deployment.image && repositoryFromReference(deployment.image) === repository.name);
            return <article className="asset-card" key={reference}>
              <div className="asset-title"><div className="asset-symbol" aria-hidden="true"><Image size={17} /></div><div><h3 className="mono">{repository.name}</h3><span>{tag ? t("Deployable image") : t("Empty repository")}</span></div></div>
              {tag ? <><div className="tag-row"><span className="tag-chip mono">{tag.tag}</span><CopyButton value={`${repository.name}:${tag.tag}`} label={t("Copy image reference")} /></div><dl className="asset-meta"><div><dt>{t("Digest")}</dt><dd className="mono copy-line"><span title={tag.digest || undefined}>{shortDigest(tag.digest)}</span>{tag.digest ? <CopyButton value={tag.digest} label={t("Copy digest")} /> : null}</dd></div><div><dt>{t("Deployment reference")}</dt><dd>{usage.length ? t("{count} current deployments", { count: usage.length }) : t("Not currently associated")}</dd></div></dl></> : <div className="orphan-note"><PackageSearch size={17} /><span>{repository.error || t("The repository exists, but Registry returned no usable tags. The build may have been interrupted or the artifact cleaned up.")}</span></div>}
            </article>;
          })}</div> : <EmptyState icon={<Image size={22} />} title={query ? t("No matching Registry artifacts") : t("Workload Registry is empty")} description={registryError || t("After a source build is pushed successfully, its repository, tag, and digest appear here.")} />
        ) : null}

        {!loading && tab === "references" ? (
          visibleReferences.length ? <div className="reference-list">{visibleReferences.map(({ reference, rows }) => (
            <article className="reference-card" key={reference}>
              <div className="reference-heading"><div><span className="source-chip">{referenceOrigin(reference, registryRepositories.has(repositoryFromReference(reference)), t)}</span><h3 className="mono">{reference}</h3></div><CopyButton value={reference} label={t("Copy the full image reference")} /></div>
              <div className="reference-deployments">{rows.map((deployment) => <span key={deployment.name || `${deployment.merchantId}/${deployment.userId}/${deployment.serviceName}`}><CircleCheck size={14} />{deployment.serviceName}<small>{deployment.merchantId}/{deployment.userId}</small></span>)}</div>
            </article>
          ))}</div> : <EmptyState icon={<Boxes size={22} />} title={query ? t("No matching deployment image") : t("No deployment image reference")} description={t("This is from the deployment snapshot, including local Registry and external Registry, not equal to the local repository directory.")} />
        ) : null}
      </section>
    </div>
  );
}
