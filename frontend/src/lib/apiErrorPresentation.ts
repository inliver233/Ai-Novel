import type { ToastApi } from "../components/ui/toast";
import type { ApiError } from "../services/apiClient";
import { toApiError, type ApiErrorFallback } from "../services/apiError";

export function formatApiErrorFields(error: Pick<ApiError, "code" | "message">): string {
  return `${error.message} (${error.code})`;
}

export function formatApiError(error: unknown): string {
  const normalized = toApiError(error);
  return formatApiErrorFields(normalized);
}

export function getApiErrorRequestId(error: unknown): string | undefined {
  const normalized = toApiError(error);
  return normalized.requestId && normalized.requestId !== "unknown" ? normalized.requestId : undefined;
}

export function toastApiError(
  toast: Pick<ToastApi, "toastError">,
  error: unknown,
  fallback?: ApiErrorFallback,
): ApiError {
  const normalized = toApiError(error, fallback);
  toast.toastError(formatApiError(normalized), getApiErrorRequestId(normalized));
  return normalized;
}
