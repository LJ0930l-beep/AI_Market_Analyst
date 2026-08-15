import { useCallback, useEffect, useState } from "react";

export type AsyncResourceStatus = "idle" | "loading" | "ready" | "unavailable";

export interface AsyncResource<T> {
  status: AsyncResourceStatus;
  data?: T;
  error?: unknown;
  retry: () => void;
}

export type ResourceLoader<T> = (signal: AbortSignal) => Promise<T>;

interface ResourceState<T> {
  status: AsyncResourceStatus;
  data?: T;
  error?: unknown;
}

function isAbortError(error: unknown): boolean {
  return error instanceof Error && error.name === "AbortError";
}

export function useAsyncResource<T>(loader: ResourceLoader<T>, enabled = true): AsyncResource<T> {
  const [attempt, setAttempt] = useState(0);
  const [state, setState] = useState<ResourceState<T>>({ status: enabled ? "loading" : "idle" });
  const retry = useCallback(() => setAttempt((value) => value + 1), []);

  useEffect(() => {
    if (!enabled) {
      setState({ status: "idle" });
      return;
    }

    const controller = new AbortController();
    let active = true;
    setState({ status: "loading" });

    void loader(controller.signal)
      .then((data) => {
        if (active && !controller.signal.aborted) {
          setState({ status: "ready", data });
        }
      })
      .catch((error: unknown) => {
        if (active && !controller.signal.aborted && !isAbortError(error)) {
          setState({ status: "unavailable", error });
        }
      });

    return () => {
      active = false;
      controller.abort();
    };
  }, [attempt, enabled, loader]);

  return { ...state, retry };
}
