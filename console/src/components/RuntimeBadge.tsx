import { CircleDashed, Moon, Play } from "lucide-react";
import { runtimeLabel } from "../format";
import { useI18n } from "../i18n";

/**
* Copy running state. Deliberately not called phase: hibernation is a normal convergence result, not a deployment life cycle phase.
 */
export function RuntimeBadge({
  runtime,
  replicas,
}: {
  runtime?: string;
  replicas?: number | null;
}) {
  const { t } = useI18n();
  const state = runtime || "Unknown";
  const icon =
    state === "Active" ? <Play size={13} />
      : state === "Dormant" ? <Moon size={13} />
        : <CircleDashed size={13} />;
  const replicaText = replicas === undefined || replicas === null
    ? t("Replica count not reported")
    : t(replicas === 1 ? "{count} replica" : "{count} replicas", { count: replicas });
  return (
    <span
      className={`runtime-chip runtime-${state.toLowerCase()}`}
      title={`${runtimeLabel(state, t)} · ${replicaText}`}
    >
      {icon}
      {runtimeLabel(state, t)}
      <small>{replicaText}</small>
    </span>
  );
}
