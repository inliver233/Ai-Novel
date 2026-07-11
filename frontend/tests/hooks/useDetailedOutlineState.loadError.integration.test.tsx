// @vitest-environment jsdom
import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { useDetailedOutlineState } from "@/pages/outline/useDetailedOutlineState";
import { ApiError } from "@/services/apiClient";
import type { DetailedOutline, DetailedOutlineListItem } from "@/services/detailedOutlinesApi";

const mocks = vi.hoisted(() => ({
  apiJson: vi.fn(),
  toast: { toastError: vi.fn(), toastSuccess: vi.fn() },
  confirm: { confirm: vi.fn() },
}));

vi.mock("@/components/ui/toast", () => ({ useToast: () => mocks.toast }));
vi.mock("@/components/ui/confirm", () => ({ useConfirm: () => mocks.confirm }));
vi.mock("@/services/apiClient", async (importOriginal) => {
  const original = (await importOriginal()) as typeof import("@/services/apiClient");
  return { ...original, apiJson: mocks.apiJson };
});

function item(id = "detail-1", outlineId = "outline-1"): DetailedOutlineListItem {
  return {
    id,
    outline_id: outlineId,
    volume_number: 1,
    volume_title: "第一卷",
    status: "planned",
    chapter_count: 0,
    updated_at: "2026-07-12T00:00:00Z",
  };
}

function detail(id = "detail-1", outlineId = "outline-1"): DetailedOutline {
  return {
    id,
    outline_id: outlineId,
    project_id: "project-1",
    volume_number: 1,
    volume_title: "第一卷",
    content_md: "旧内容",
    structure: null,
    status: "planned",
    created_at: "2026-07-12T00:00:00Z",
    updated_at: "2026-07-12T00:00:00Z",
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

describe("useDetailedOutlineState load errors", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("contains an initial list rejection and succeeds on retry without toast or unhandled rejection", async () => {
    const unhandled = vi.fn();
    window.addEventListener("unhandledrejection", unhandled);
    const error = new ApiError({ code: "DETAIL_LIST_FAILED", message: "列表失败", requestId: "rid-list", status: 503 });
    mocks.apiJson.mockRejectedValueOnce(error).mockResolvedValueOnce({ data: { detailed_outlines: [item()] } });

    const { result } = renderHook(() => useDetailedOutlineState("project-1", "outline-1"));

    await waitFor(() => expect(result.current.error).toBe(error));
    expect(result.current.hasLoaded).toBe(true);
    expect(result.current.hasData).toBe(false);

    await act(async () => {
      await result.current.reload();
    });

    expect(result.current.error).toBeNull();
    expect(result.current.hasData).toBe(true);
    expect(result.current.items).toEqual([item()]);
    expect(mocks.toast.toastError).not.toHaveBeenCalled();
    expect(unhandled).not.toHaveBeenCalled();
    window.removeEventListener("unhandledrejection", unhandled);
  });

  it("retains stale list data when refresh fails", async () => {
    mocks.apiJson.mockResolvedValueOnce({ data: { detailed_outlines: [item()] } });
    const { result } = renderHook(() => useDetailedOutlineState("project-1", "outline-1"));
    await waitFor(() => expect(result.current.hasData).toBe(true));

    const error = new ApiError({
      code: "DETAIL_REFRESH_FAILED",
      message: "刷新失败",
      requestId: "rid-refresh",
      status: 503,
    });
    mocks.apiJson.mockRejectedValueOnce(error);
    await act(async () => {
      await result.current.reload();
    });

    expect(result.current.error).toBe(error);
    expect(result.current.items).toEqual([item()]);
    expect(result.current.hasData).toBe(true);
  });

  it("ignores an old outline request failure after switching outlines", async () => {
    const oldRequest = deferred<DetailedOutlineListItem[]>();
    mocks.apiJson.mockImplementation((url: string) =>
      url.includes("/outline-a/")
        ? oldRequest.promise.then((items) => ({ data: { detailed_outlines: items } }))
        : Promise.resolve({ data: { detailed_outlines: [item("detail-b", "outline-b")] } }),
    );

    const { result, rerender } = renderHook(({ outlineId }) => useDetailedOutlineState("project-1", outlineId), {
      initialProps: { outlineId: "outline-a" },
    });
    rerender({ outlineId: "outline-b" });
    await waitFor(() => expect(result.current.items[0]?.id).toBe("detail-b"));

    await act(async () => {
      oldRequest.reject(
        new ApiError({ code: "OLD_FAILURE", message: "旧请求失败", requestId: "rid-old", status: 503 }),
      );
      await Promise.resolve();
    });

    expect(result.current.error).toBeNull();
    expect(result.current.items[0]?.outline_id).toBe("outline-b");
  });

  it("keeps the selected list item and retries an inline detail failure", async () => {
    mocks.apiJson.mockResolvedValueOnce({ data: { detailed_outlines: [item()] } });
    const error = new ApiError({ code: "DETAIL_FAILED", message: "详情失败", requestId: "rid-detail", status: 503 });
    mocks.apiJson.mockRejectedValueOnce(error).mockResolvedValueOnce({ data: { detailed_outline: detail() } });
    const { result } = renderHook(() => useDetailedOutlineState("project-1", "outline-1"));
    await waitFor(() => expect(result.current.hasData).toBe(true));

    await act(async () => {
      await result.current.selectVolume("detail-1");
    });
    expect(result.current.selectedId).toBe("detail-1");
    expect(result.current.detailError).toBe(error);
    expect(result.current.detailHasData).toBe(false);

    await act(async () => {
      await result.current.reloadDetail();
    });
    expect(result.current.selected).toEqual(detail());
    expect(result.current.detailError).toBeNull();
    expect(result.current.detailHasData).toBe(true);
    expect(mocks.toast.toastError).not.toHaveBeenCalled();
  });

  it("does not let an in-flight detail reload overwrite edits started after the request", async () => {
    const reloaded = deferred<DetailedOutline>();
    mocks.apiJson
      .mockResolvedValueOnce({ data: { detailed_outlines: [item()] } })
      .mockResolvedValueOnce({ data: { detailed_outline: detail() } })
      .mockReturnValueOnce(reloaded.promise.then((value) => ({ data: { detailed_outline: value } })));
    const { result } = renderHook(() => useDetailedOutlineState("project-1", "outline-1"));
    await waitFor(() => expect(result.current.hasData).toBe(true));
    await act(async () => {
      await result.current.selectVolume("detail-1");
    });

    let pending!: Promise<void>;
    act(() => {
      pending = result.current.reloadDetail();
    });
    act(() => {
      result.current.startEdit();
      result.current.setEditContent("本地编辑");
    });
    await act(async () => {
      reloaded.resolve({ ...detail(), content_md: "服务端新内容" });
      await pending;
    });

    expect(result.current.editing).toBe(true);
    expect(result.current.editContent).toBe("本地编辑");
    expect(result.current.selected?.content_md).toBe("旧内容");
  });

  it("keeps the active volume and local edits when another volume is selected during editing", async () => {
    mocks.apiJson
      .mockResolvedValueOnce({ data: { detailed_outlines: [item(), item("detail-2")] } })
      .mockResolvedValueOnce({ data: { detailed_outline: detail() } });
    const { result } = renderHook(() => useDetailedOutlineState("project-1", "outline-1"));
    await waitFor(() => expect(result.current.hasData).toBe(true));
    await act(async () => {
      await result.current.selectVolume("detail-1");
    });
    act(() => {
      result.current.startEdit();
      result.current.setEditContent("未保存的本地内容");
    });

    await act(async () => {
      await result.current.selectVolume("detail-2");
    });

    expect(mocks.apiJson).toHaveBeenCalledTimes(2);
    expect(result.current.selectedId).toBe("detail-1");
    expect(result.current.selected?.id).toBe("detail-1");
    expect(result.current.editing).toBe(true);
    expect(result.current.editContent).toBe("未保存的本地内容");
  });
});
