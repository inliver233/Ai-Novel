import type { ToastApi } from "../components/ui/toast";
import type { ApiError } from "../services/apiClient";

export function formatApiError(error: ApiError): string {
  return `${error.message} (${error.code})`;
}

export function getApiErrorRequestId(error: ApiError): string | undefined {
  return error.requestId && error.requestId !== "unknown" ? error.requestId : undefined;
}

export function toastApiError(toast: Pick<ToastApi, "toastError">, error: ApiError): void {
  toast.toastError(formatApiError(error), getApiErrorRequestId(error));
}
