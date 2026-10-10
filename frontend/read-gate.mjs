// A response may arrive after cancellation, a new run, or a newer state request.
export function createReadGate(currentRunId) {
  let generation = 0;
  let active = null;

  return {
    invalidate() { generation += 1; },
    async fetch(runId, request) {
      if (active?.generation === generation && active.runId === runId) return { kind: "overlap" };
      const token = { generation, runId };
      active = token;
      try {
        const value = await request(runId);
        return token.generation === generation && currentRunId() === runId
          ? { kind: "current", value } : { kind: "stale" };
      } catch (error) {
        return token.generation === generation && currentRunId() === runId
          ? { kind: "error", error } : { kind: "stale" };
      } finally {
        if (active === token) active = null;
      }
    },
  };
}

export function regressesTerminalState(previous, next) {
  return previous?.run_id === next?.run_id &&
    ["succeeded", "uncertain", "stopped", "failed"].includes(previous.status) &&
    !["succeeded", "uncertain", "stopped", "failed"].includes(next.status);
}
