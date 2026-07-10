import { useCallback, useLayoutEffect, useRef, useState } from "react";

export type QueuedSaveController<TArgs extends unknown[]> = {
  save: (...args: TArgs) => Promise<boolean>;
  saving: boolean;
};

export function useQueuedSave<TArgs extends unknown[]>(
  saveFn: (...args: TArgs) => Promise<boolean>,
): QueuedSaveController<TArgs> {
  const saveFnRef = useRef(saveFn);
  const savingRef = useRef(false);
  const queuedArgsRef = useRef<TArgs | null>(null);
  const [saving, setSaving] = useState(false);

  useLayoutEffect(() => {
    saveFnRef.current = saveFn;
  }, [saveFn]);

  const save = useCallback(async (...args: TArgs): Promise<boolean> => {
    if (savingRef.current) {
      queuedArgsRef.current = args;
      return false;
    }

    savingRef.current = true;
    setSaving(true);

    const invokeSave = (nextArgs: TArgs): Promise<boolean> => {
      try {
        return saveFnRef.current(...nextArgs);
      } catch (error) {
        return Promise.reject(error);
      }
    };

    const drain = (): void => {
      const queuedArgs = queuedArgsRef.current;
      queuedArgsRef.current = null;
      if (queuedArgs !== null) {
        const queuedSave = invokeSave(queuedArgs);
        void queuedSave.then(drain, drain);
        return;
      }

      savingRef.current = false;
      setSaving(false);
    };

    const currentSave = invokeSave(args);
    void currentSave.then(drain, drain);
    return currentSave;
  }, []);

  return { save, saving };
}
