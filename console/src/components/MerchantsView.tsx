import {
  Ban,
  KeyRound,
  LoaderCircle,
  Pencil,
  Plus,
  Users,
} from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import { formatCount, relativeTime } from "../format";
import { useI18n } from "../i18n";
import type { MerchantKeyResponse, MerchantView } from "../types";
import { EmptyState, PageHeader, RefreshButton, SearchField } from "./ConsolePrimitives";
import SecretDialog from "./SecretDialog";

/**
* Merchant page: list, new, quota change, key rotation, deactivation.
*
* Deliberately not automatically polling: This page is almost entirely write operations, and background re-pulling will replace rows during the form filling process.
* Data freshness depends on re-pull + manual refresh button after each write operation.
*
* `GET /v1/merchants/{id}` (Details endpoint of contract §4.3) This page does not call: its "name tenant"
* List" is exactly the same as `GET /v1/tenants?merchantId=` on the tenant page. This page uses jump to it instead.
* One less responsive shape that needs to be aligned on both sides.
 */

const DEFAULT_MAX_TENANTS = 5;
const DEFAULT_MAX_DEPLOYMENTS = 10;

interface QuotaDraft {
  displayName: string;
  maxTenants: string;
  maxDeployments: string;
  /** Three segments of ResourceQuota. When the string is stored: the value is the Kubernetes dimension (`4` / `500m` /
* `2Gi`), the frontend does not parse or convert, it displays and returns the original. */
  cpu: string;
  memory: string;
  pods: string;
}

/** For merchants that have not been separately configured, this file will be echoed by the server, so it will not be used normally - it is reserved for the server.
* The backend when tenantQuota is not echoed (the old backend/field is removed). */
const FALLBACK_QUOTA = { cpu: "4", memory: "4Gi", pods: "16" };

export default function MerchantsView({
  onError,
  onInspectTenants,
}: {
  onError: (cause: unknown) => void;
  onInspectTenants: (merchantId: string) => void;
}) {
  const { localeTag, t } = useI18n();
  const [merchants, setMerchants] = useState<MerchantView[]>([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [query, setQuery] = useState("");
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);
  const [busy, setBusy] = useState("");
  const [creating, setCreating] = useState(false);
  const [formError, setFormError] = useState("");
  const [draftId, setDraftId] = useState("");
  const [draftName, setDraftName] = useState("");
  const [draftTenants, setDraftTenants] = useState(String(DEFAULT_MAX_TENANTS));
  const [draftDeployments, setDraftDeployments] = useState(String(DEFAULT_MAX_DEPLOYMENTS));
  const [editing, setEditing] = useState("");
  const [edit, setEdit] = useState<QuotaDraft>({
    displayName: "",
    maxTenants: "",
    maxDeployments: "",
    cpu: "",
    memory: "",
    pods: "",
  });
  // The plaintext key only lives in this state, and the pop-up window disappears as soon as it is closed. No storage is entered.
  const [issued, setIssued] = useState<MerchantKeyResponse | null>(null);

  const load = useCallback(async (silent = false) => {
    if (silent) setRefreshing(true);
    try {
      const response = await api.listMerchants();
      setMerchants(response.merchants ?? []);
      setLastUpdated(new Date());
    } catch (cause) {
      onError(cause);
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, [onError]);

  useEffect(() => { void load(); }, [load]);

  const submitCreate = async (event: React.FormEvent) => {
    event.preventDefault();
    const merchantId = draftId.trim();
    const displayName = draftName.trim();
    const maxTenants = Number(draftTenants);
    const maxDeployments = Number(draftDeployments);
    // The server will use `normalize_merchant_id` to verify again and give the authoritative copy, which only blocks
    // If it is obviously filled in incorrectly, avoid making a round trip for an empty field.
    if (!merchantId || !displayName) {
      setFormError(t("Merchant ID and display name are required."));
      return;
    }
    if (!Number.isInteger(maxTenants) || maxTenants < 1
      || !Number.isInteger(maxDeployments) || maxDeployments < 1) {
      setFormError(t("Both quotas must be integers no less than 1."));
      return;
    }
    setFormError("");
    setBusy("__create__");
    try {
      const response = await api.createMerchant({
        merchantId,
        displayName,
        maxTenants,
        maxDeployments,
      });
      setIssued(response);
      setCreating(false);
      setDraftId("");
      setDraftName("");
      setDraftTenants(String(DEFAULT_MAX_TENANTS));
      setDraftDeployments(String(DEFAULT_MAX_DEPLOYMENTS));
      await load();
    } catch (cause) {
      setFormError(cause instanceof Error ? cause.message : String(cause));
      onError(cause);
    } finally {
      setBusy("");
    }
  };

  const startEdit = (merchant: MerchantView) => {
    setEditing(merchant.merchantId);
    const quota = merchant.tenantQuota ?? FALLBACK_QUOTA;
    setEdit({
      displayName: merchant.displayName,
      maxTenants: String(merchant.maxTenants),
      maxDeployments: String(merchant.maxDeployments),
      cpu: quota.cpu,
      memory: quota.memory,
      pods: quota.pods,
    });
  };

  const submitEdit = async (merchantId: string) => {
    const maxTenants = Number(edit.maxTenants);
    const maxDeployments = Number(edit.maxDeployments);
    if (!Number.isInteger(maxTenants) || maxTenants < 1
      || !Number.isInteger(maxDeployments) || maxDeployments < 1) {
      onError(new Error(t("Quota must be an integer no less than 1.")));
      return;
    }
    const quota = {
      cpu: edit.cpu.trim(),
      memory: edit.memory.trim(),
      pods: edit.pods.trim(),
    };
    if (!quota.cpu || !quota.memory || !quota.pods) {
      onError(new Error(t("All three resource limit items must be filled in.")));
      return;
    }
    setBusy(merchantId);
    try {
      await api.updateMerchant(merchantId, {
        displayName: edit.displayName.trim(),
        maxTenants,
        maxDeployments,
        // Dimensions are not verified on the frontend: the legality of `4` / `500m` / `2Gi` is determined by Kubernetes.
        // Writing another set of regular expressions here will only drift away from it. The consequence of incorrect writing is that ResourceQuota is
        // apiserver rejects, that error will appear in the reconcile log.
        tenantQuota: quota,
      });
      setEditing("");
      await load();
    } catch (cause) {
      onError(cause);
    } finally {
      setBusy("");
    }
  };

  const rotate = async (merchant: MerchantView) => {
    if (!window.confirm(
      t("Rotate the API key for merchant \"{name}\"? The old key becomes invalid immediately, and every integration using it will receive 401 responses.", {
        name: merchant.displayName,
      }),
    )) return;
    setBusy(merchant.merchantId);
    try {
      setIssued(await api.rotateMerchantKey(merchant.merchantId));
    } catch (cause) {
      onError(cause);
    } finally {
      setBusy("");
    }
  };

  const disable = async (merchant: MerchantView) => {
    if (!window.confirm(
      t("Disable merchant \"{name}\"? Tokens for all tenants under it also become invalid, but deployed sites are not deleted.", {
        name: merchant.displayName,
      }),
    )) return;
    setBusy(merchant.merchantId);
    try {
      await api.disableMerchant(merchant.merchantId);
      await load();
    } catch (cause) {
      onError(cause);
    } finally {
      setBusy("");
    }
  };

  const normalizedQuery = query.trim().toLowerCase();
  const visibleMerchants = merchants.filter((merchant) => !normalizedQuery
    || `${merchant.merchantId} ${merchant.displayName}`.toLowerCase().includes(normalizedQuery));
  const activeCount = merchants.filter((merchant) => !merchant.disabledAt).length;

  return (
    <div className="page">
      <PageHeader
        eyebrow={t("IDENTITY & QUOTAS")}
        title={t("Merchants")}
        description={t("Manage first-tier identities, total platform quotas, and resource packages for each tenant namespace. Deactivation only revokes access and does not delete existing workloads.")}
        meta={<span aria-live="polite">{t("{enabled} enabled · {disabled} disabled", { enabled: activeCount, disabled: merchants.length - activeCount })}{lastUpdated ? ` · ${t("Updated {time}", { time: lastUpdated.toLocaleTimeString(localeTag) })}` : ""}</span>}
        actions={<div className="page-actions"><RefreshButton refreshing={loading || refreshing} onRefresh={() => void load(true)} />
            <button
              type="button"
              className="button button-primary"
              onClick={() => { setCreating((value) => !value); setFormError(""); }}
            >
              <Plus size={15} />{t("Create a new merchant")}
            </button>
          </div>}
      />

      <section className="workspace-card">
        <div className="workspace-toolbar">
          <SearchField value={query} onChange={setQuery} label={t("Search for merchants")} placeholder={t("Search for merchant ID or display name")} />
          <span className="card-count">{t("Showing {visible} / {total}", { visible: visibleMerchants.length, total: merchants.length })}</span>
        </div>

        {creating ? (
          <form className="form-panel" onSubmit={(event) => void submitCreate(event)}>
            <label className="field">
              <span>{t("Merchant ID")}</span>
              <input
                value={draftId}
                placeholder={t("1-31 lowercase letters, numbers, or hyphens")}
                onChange={(event) => setDraftId(event.target.value)}
              />
            </label>
            <label className="field">
              <span>{t("Display name")}</span>
              <input
                value={draftName}
                onChange={(event) => setDraftName(event.target.value)}
              />
            </label>
            <label className="field field-narrow">
              <span>{t("Tenant cap")}</span>
              <input
                type="number"
                min={1}
                value={draftTenants}
                onChange={(event) => setDraftTenants(event.target.value)}
              />
            </label>
            <label className="field field-narrow">
              <span>{t("Deployment cap")}</span>
              <input
                type="number"
                min={1}
                value={draftDeployments}
                onChange={(event) => setDraftDeployments(event.target.value)}
              />
            </label>
            <div className="form-actions">
              <button type="submit" className="button button-primary" disabled={busy === "__create__"}>
                {busy === "__create__" ? <LoaderCircle className="spin" size={16} /> : null}
                {t("Create and issue key")}
              </button>
              <button type="button" className="button" onClick={() => setCreating(false)}>{t("Cancel")}</button>
            </div>
            {formError ? <p className="form-error" role="alert">{formError}</p> : null}
            <p className="form-note">
              {t("After creation, the plaintext API key is only displayed once, and the control plane only saves its sha256 digest.")}
            </p>
          </form>
        ) : null}

        {loading ? (
          <div className="state"><LoaderCircle className="spin" size={20} /><strong>{t("Loading")}</strong></div>
        ) : visibleMerchants.length === 0 ? (
          <EmptyState icon={<Users size={22} />} title={query ? t("No matching merchants") : t("No merchant yet")} description={query ? t("Adjust your search terms and try again.") : t("Tenant tokens can be issued and deployment quotas allocated after a new merchant is created.")} />
        ) : (
          <div className="table-scroll responsive-table">
            <table className="data-table">
              <thead>
                <tr>
                  <th>{t("Merchant ID")}</th>
                  <th>{t("Display name")}</th>
                  <th>{t("Tenant usage")}</th>
                  <th>{t("Deployment usage")}</th>
                  <th>{t("Resource cap")}<small>{t("CPU/Memory/Pod")}</small></th>
                  <th>{t("Created")}</th>
                  <th>{t("Status")}</th>
                  <th aria-label={t("Operation")} />
                </tr>
              </thead>
              <tbody>
                {visibleMerchants.map((merchant) => {
                  const rowBusy = busy === merchant.merchantId;
                  const isEditing = editing === merchant.merchantId;
                  const disabled = Boolean(merchant.disabledAt);
                  return (
                    <tr key={merchant.merchantId} className={disabled ? "row-disabled" : undefined}>
                      <td className="mono" data-label={t("Merchant ID")}>{merchant.merchantId}</td>
                      <td data-label={t("Display name")}>
                        {isEditing ? (
                          <input
                            className="cell-input"
                            value={edit.displayName}
                            onChange={(event) =>
                              setEdit((value) => ({ ...value, displayName: event.target.value }))}
                          />
                        ) : (
                          merchant.displayName
                        )}
                      </td>
                      <td className="mono" data-label={t("Tenant usage")}>
                        {isEditing ? (
                          <input
                            className="cell-input cell-input-number"
                            type="number"
                            min={1}
                            value={edit.maxTenants}
                            onChange={(event) =>
                              setEdit((value) => ({ ...value, maxTenants: event.target.value }))}
                          />
                        ) : (
                          `${formatCount(merchant.tenantCount, t)} / ${merchant.maxTenants}`
                        )}
                      </td>
                      <td className="mono" data-label={t("Deployment usage")}>
                        {isEditing ? (
                          <input
                            className="cell-input cell-input-number"
                            type="number"
                            min={1}
                            value={edit.maxDeployments}
                            onChange={(event) =>
                              setEdit((value) => ({ ...value, maxDeployments: event.target.value }))}
                          />
                        ) : (
                          `${formatCount(merchant.deploymentCount, t)} / ${merchant.maxDeployments}`
                        )}
                      </td>
                      <td className="mono" data-label={t("Resource cap")}>
                        {isEditing ? (
                          <span className="quota-inputs">
                            <input
                              className="cell-input cell-input-quota"
                              aria-label={t("CPU cap")}
                              value={edit.cpu}
                              onChange={(event) =>
                                setEdit((value) => ({ ...value, cpu: event.target.value }))}
                            />
                            <input
                              className="cell-input cell-input-quota"
                              aria-label={t("Memory limit")}
                              value={edit.memory}
                              onChange={(event) =>
                                setEdit((value) => ({ ...value, memory: event.target.value }))}
                            />
                            <input
                              className="cell-input cell-input-quota"
                              aria-label={t("Maximum number of Pods")}
                              value={edit.pods}
                              onChange={(event) =>
                                setEdit((value) => ({ ...value, pods: event.target.value }))}
                            />
                          </span>
                        ) : (
                          `${merchant.tenantQuota?.cpu ?? "—"} / ${
                            merchant.tenantQuota?.memory ?? "—"} / ${
                            merchant.tenantQuota?.pods ?? "—"}`
                        )}
                      </td>
                      <td data-label={t("Created")}>{relativeTime(merchant.createdAt, localeTag, t)}</td>
                      <td data-label={t("Status")}>
                        <span className={`badge ${disabled ? "badge-bad" : "badge-ok"}`}>
                          {disabled ? t("Disabled") : t("Enabled")}
                        </span>
                      </td>
                      <td className="cell-actions" data-label={t("Operation")}>
                        {isEditing ? (
                          <>
                            <button
                              type="button"
                              className="button button-small button-primary"
                              disabled={rowBusy}
                              onClick={() => void submitEdit(merchant.merchantId)}
                            >
                              {rowBusy ? <LoaderCircle className="spin" size={14} /> : null}{t("Save")}
                            </button>
                            <button
                              type="button"
                              className="button button-small"
                              onClick={() => setEditing("")}
                            >
                              {t("Cancel")}
                            </button>
                          </>
                        ) : (
                          <>
                            <button
                              type="button"
                              className="button button-small"
                              onClick={() => onInspectTenants(merchant.merchantId)}
                            >
                              <Users size={14} />{t("Tenants")}
                            </button>
                            <button
                              type="button"
                              className="button button-small"
                              onClick={() => startEdit(merchant)}
                            >
                              <Pencil size={14} />{t("Change quota")}
                            </button>
                            <button
                              type="button"
                              className="button button-small"
                              disabled={rowBusy}
                              onClick={() => void rotate(merchant)}
                            >
                              <KeyRound size={14} />{t("Rotate key")}
                            </button>
                            <button
                              type="button"
                              className="button button-small button-danger"
                              disabled={rowBusy || disabled}
                              onClick={() => void disable(merchant)}
                            >
                              <Ban size={14} />{t("Deactivate")}
                            </button>
                          </>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {issued ? (
        <SecretDialog
          title={t("Merchant API key")}
          subject={t("Merchant {id}", { id: issued.merchantId })}
          secret={issued.apiKey}
          shownOnce={issued.apiKeyShownOnce}
          onClose={() => setIssued(null)}
        />
      ) : null}
    </div>
  );
}
