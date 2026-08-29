import { useEffect, useState } from "react";
import type { LiveState } from "../types";

const EMPTY: LiveState = { jobs: [], projects: [], segments: [] };

/**
 * Подписка на серверный поток состояния. Переподключение берёт на себя
 * EventSource — сервер присылает retry, поэтому руками ничего не нужно.
 */
export function useLiveState(): { state: LiveState; connected: boolean } {
  const [state, setState] = useState<LiveState>(EMPTY);
  const [connected, setConnected] = useState(false);

  useEffect(() => {
    const source = new EventSource("/api/events/stream");

    source.addEventListener("state", (event) => {
      setConnected(true);
      try {
        setState(JSON.parse((event as MessageEvent).data));
      } catch {
        /* повреждённый кадр — ждём следующий */
      }
    });
    source.onopen = () => setConnected(true);
    source.onerror = () => setConnected(false);

    return () => source.close();
  }, []);

  return { state, connected };
}

export function useAsync<T>(
  loader: () => Promise<T>,
  deps: unknown[] = [],
): { data: T | null; error: string; loading: boolean; reload: () => void } {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [nonce, setNonce] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    loader()
      .then((result) => {
        if (!cancelled) {
          setData(result);
          setError("");
        }
      })
      .catch((exc: Error) => {
        if (!cancelled) setError(exc.message);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, nonce]);

  return { data, error, loading, reload: () => setNonce((n) => n + 1) };
}
