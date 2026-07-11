import { copyText } from "../../lib/copyText";
import { formatApiErrorFields } from "../../lib/apiErrorPresentation";
import type { ApiError } from "../../services/apiClient";

export function QueryErrorCard(props: {
  error: ApiError;
  onRetry: () => void;
  retryBlockedReason?: string;
  title?: string;
  variant?: "blocking" | "warning";
}) {
  const requestId = props.error.requestId && props.error.requestId !== "unknown" ? props.error.requestId : undefined;
  const blocking = (props.variant ?? "blocking") === "blocking";
  return (
    <div className={blocking ? "error-card" : "rounded-atelier border border-warning/30 bg-warning/5 p-4"}>
      <div className="state-title">{props.title ?? (blocking ? "加载失败" : "最近一次刷新失败")}</div>
      <div className="state-desc">{formatApiErrorFields(props.error)}</div>
      {requestId ? (
        <div className="mt-3 flex flex-wrap items-center gap-2 text-xs text-subtext">
          <span>request_id: {requestId}</span>
          <button
            className="btn btn-secondary btn-sm"
            onClick={() => void copyText(requestId, { title: "复制 request_id" })}
            type="button"
          >
            复制 request_id
          </button>
        </div>
      ) : null}
      {props.retryBlockedReason ? (
        <div className="mt-3 text-xs text-warning">{props.retryBlockedReason}</div>
      ) : (
        <button className="btn btn-primary mt-4" onClick={props.onRetry} type="button">
          重试
        </button>
      )}
    </div>
  );
}
