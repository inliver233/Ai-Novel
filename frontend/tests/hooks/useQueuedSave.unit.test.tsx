// @vitest-environment jsdom
import { act, renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { useQueuedSave } from "@/hooks/useQueuedSave";

type Deferred<T> = {
  promise: Promise<T>;
  reject: (reason?: unknown) => void;
  resolve: (value: T) => void;
};

function deferred<T>(): Deferred<T> {
  let reject!: Deferred<T>["reject"];
  let resolve!: Deferred<T>["resolve"];
  const promise = new Promise<T>((promiseResolve, promiseReject) => {
    resolve = promiseResolve;
    reject = promiseReject;
  });
  return { promise, reject, resolve };
}

describe("useQueuedSave", () => {
  it("runs an idle save and exposes its saving state", async () => {
    const pending = deferred<boolean>();
    const saveFn = vi.fn<(value: string) => Promise<boolean>>(() => pending.promise);
    const { result } = renderHook(() => useQueuedSave(saveFn));

    let savePromise!: Promise<boolean>;
    act(() => {
      savePromise = result.current.save("draft");
    });

    expect(saveFn).toHaveBeenCalledWith("draft");
    expect(result.current.saving).toBe(true);

    let saved: boolean | undefined;
    await act(async () => {
      pending.resolve(true);
      saved = await savePromise;
    });

    expect(saved).toBe(true);
    expect(result.current.saving).toBe(false);
  });

  it("keeps one save in flight and drains only the latest queued arguments", async () => {
    const pending = [deferred<boolean>(), deferred<boolean>()];
    const calls: string[] = [];
    const saveFn = vi.fn((value: string) => {
      calls.push(value);
      return pending[calls.length - 1].promise;
    });
    const { result } = renderHook(() => useQueuedSave(saveFn));

    let firstSave!: Promise<boolean>;
    let firstResult: boolean | undefined;
    act(() => {
      firstSave = result.current.save("A");
    });

    let queuedB: boolean | undefined;
    let queuedC: boolean | undefined;
    await act(async () => {
      queuedB = await result.current.save("B");
      queuedC = await result.current.save("C");
    });

    expect(queuedB).toBe(false);
    expect(queuedC).toBe(false);
    expect(calls).toEqual(["A"]);

    await act(async () => {
      pending[0].resolve(true);
      firstResult = await firstSave;
    });

    expect(calls).toEqual(["A", "C"]);
    expect(firstResult).toBe(true);
    expect(result.current.saving).toBe(true);

    await act(async () => {
      pending[1].resolve(true);
      await Promise.resolve();
    });

    expect(result.current.saving).toBe(false);
  });

  it("uses the latest save function closure when draining", async () => {
    const firstPending = deferred<boolean>();
    const queuedPending = deferred<boolean>();
    const calls: string[] = [];
    const { result, rerender } = renderHook(
      ({ version }) =>
        useQueuedSave(async (value: string) => {
          calls.push(`${version}:${value}`);
          return version === 1 ? firstPending.promise : queuedPending.promise;
        }),
      { initialProps: { version: 1 } },
    );

    let firstSave!: Promise<boolean>;
    act(() => {
      firstSave = result.current.save("A");
    });

    rerender({ version: 2 });
    await expect(result.current.save("B")).resolves.toBe(false);
    rerender({ version: 3 });
    await expect(result.current.save("C")).resolves.toBe(false);

    await act(async () => {
      firstPending.resolve(true);
      await firstSave;
    });

    expect(calls).toEqual(["1:A", "3:C"]);

    await act(async () => {
      queuedPending.resolve(true);
      await Promise.resolve();
    });
  });

  it("drains after a failed result without dropping the saving state", async () => {
    const pending = [deferred<boolean>(), deferred<boolean>()];
    const saveFn = vi
      .fn<(value: string) => Promise<boolean>>()
      .mockImplementationOnce(() => pending[0].promise)
      .mockImplementationOnce(() => pending[1].promise);
    const { result } = renderHook(() => useQueuedSave(saveFn));

    let firstSave!: Promise<boolean>;
    let firstResult: boolean | undefined;
    act(() => {
      firstSave = result.current.save("A");
    });
    await expect(result.current.save("B")).resolves.toBe(false);

    await act(async () => {
      pending[0].resolve(false);
      firstResult = await firstSave;
    });

    expect(saveFn).toHaveBeenNthCalledWith(2, "B");
    expect(firstResult).toBe(false);
    expect(result.current.saving).toBe(true);

    await act(async () => {
      pending[1].resolve(true);
      await Promise.resolve();
    });

    expect(result.current.saving).toBe(false);
  });

  it("drains after a rejected save and preserves the rejection", async () => {
    const pending = [deferred<boolean>(), deferred<boolean>()];
    const error = new Error("save failed");
    const saveFn = vi
      .fn<(value: string) => Promise<boolean>>()
      .mockImplementationOnce(() => pending[0].promise)
      .mockImplementationOnce(() => pending[1].promise);
    const { result } = renderHook(() => useQueuedSave(saveFn));

    let firstSave!: Promise<boolean>;
    act(() => {
      firstSave = result.current.save("A");
    });
    await expect(result.current.save("B")).resolves.toBe(false);

    await act(async () => {
      pending[0].reject(error);
      await expect(firstSave).rejects.toBe(error);
    });

    expect(saveFn).toHaveBeenNthCalledWith(2, "B");
    expect(result.current.saving).toBe(true);

    await act(async () => {
      pending[1].resolve(true);
      await Promise.resolve();
    });

    await waitFor(() => expect(result.current.saving).toBe(false));
  });

  it("recovers when the save function throws before returning a promise", async () => {
    const error = new Error("sync failure");
    const saveFn = vi.fn<(value: string) => Promise<boolean>>(() => {
      throw error;
    });
    const { result } = renderHook(() => useQueuedSave(saveFn));

    let savePromise!: Promise<boolean>;
    act(() => {
      savePromise = result.current.save("A");
    });

    expect(result.current.saving).toBe(true);
    await act(async () => {
      await expect(savePromise).rejects.toBe(error);
    });

    expect(result.current.saving).toBe(false);
    expect(saveFn).toHaveBeenCalledOnce();
  });
});
