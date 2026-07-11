import { ApiError } from "./apiClient";

export type ApiErrorFallback = Partial<Pick<ApiError, "code" | "message" | "requestId" | "status">>;

function getThrownMessage(error: unknown): string | undefined {
  if (error instanceof Error) {
    const message = error.message.trim();
    return message || undefined;
  }
  if (typeof error === "string") {
    const message = error.trim();
    return message || undefined;
  }
  return undefined;
}

export function toApiError(error: unknown, fallback: ApiErrorFallback = {}): ApiError {
  if (error instanceof ApiError) return error;

  return new ApiError({
    code: fallback.code ?? "UNKNOWN",
    message: fallback.message ?? getThrownMessage(error) ?? "请求失败",
    requestId: fallback.requestId ?? "unknown",
    status: fallback.status ?? 0,
    details: error,
  });
}
