import {
  Check,
  Clipboard,
  LoaderCircle,
  RefreshCw,
  Search,
} from "lucide-react";
import { useState } from "react";
import { useI18n } from "../i18n";

export function PageHeader({
  eyebrow,
  title,
  description,
  actions,
  meta,
}: {
  eyebrow: string;
  title: string;
  description: string;
  actions?: React.ReactNode;
  meta?: React.ReactNode;
}) {
  return (
    <header className="page-header">
      <div className="page-header-copy">
        <span className="page-eyebrow">{eyebrow}</span>
        <h2 tabIndex={-1}>{title}</h2>
        <p>{description}</p>
        {meta ? <div className="page-meta">{meta}</div> : null}
      </div>
      {actions ? <div className="page-actions">{actions}</div> : null}
    </header>
  );
}

export function RefreshButton({
  refreshing,
  onRefresh,
  label,
}: {
  refreshing: boolean;
  onRefresh: () => void;
  label?: string;
}) {
  const { t } = useI18n();
  const text = label ?? t("Refresh");
  return (
    <button
      type="button"
      className="button button-small"
      disabled={refreshing}
      aria-busy={refreshing}
      onClick={onRefresh}
    >
      {refreshing ? <LoaderCircle className="spin" size={15} /> : <RefreshCw size={15} />}
      {refreshing ? t("Refreshing") : text}
    </button>
  );
}

export function SearchField({
  value,
  onChange,
  label,
  placeholder,
}: {
  value: string;
  onChange: (value: string) => void;
  label: string;
  placeholder: string;
}) {
  return (
    <label className="search-field">
      <span className="sr-only">{label}</span>
      <Search size={16} aria-hidden="true" />
      <input
        type="search"
        value={value}
        placeholder={placeholder}
        onChange={(event) => onChange(event.target.value)}
      />
    </label>
  );
}

export function MetricCard({
  label,
  value,
  hint,
  icon,
  tone = "neutral",
  onClick,
}: {
  label: string;
  value: string | number;
  hint: string;
  icon: React.ReactNode;
  tone?: "neutral" | "good" | "warn" | "bad";
  onClick?: () => void;
}) {
  const content = (
    <>
      <span className={`metric-icon metric-${tone}`} aria-hidden="true">{icon}</span>
      <span className="metric-label">{label}</span>
      <strong className="metric-value">{value}</strong>
      <span className="metric-hint">{hint}</span>
    </>
  );
  return onClick ? (
    <button type="button" className="metric-card metric-action" onClick={onClick}>
      {content}
    </button>
  ) : <article className="metric-card">{content}</article>;
}

export function CopyButton({ value, label }: { value: string; label?: string }) {
  const { t } = useI18n();
  const text = label ?? t("Copy");
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1800);
    } catch {
      setCopied(false);
    }
  };
  return (
    <button
      type="button"
      className="icon-button copy-button"
      aria-label={`${text} ${value}`}
      title={copied ? t("Copied") : text}
      onClick={() => void copy()}
    >
      {copied ? <Check size={15} /> : <Clipboard size={15} />}
      <span className="sr-only" aria-live="polite">{copied ? t("Copied") : text}</span>
    </button>
  );
}

export function EmptyState({
  icon,
  title,
  description,
  action,
}: {
  icon: React.ReactNode;
  title: string;
  description: string;
  action?: React.ReactNode;
}) {
  return (
    <div className="empty-state">
      <span className="empty-icon" aria-hidden="true">{icon}</span>
      <strong>{title}</strong>
      <p>{description}</p>
      {action ? <div>{action}</div> : null}
    </div>
  );
}
