export const store = {
  state: { jobs: [], connected: false, lastEventAt: 0 },
  listeners: new Set(),

  subscribe(listener) {
    this.listeners.add(listener);
    listener(this.state);
    return () => this.listeners.delete(listener);
  },

  setState(patch) {
    this.state = { ...this.state, ...patch };
    for (const listener of this.listeners) listener(this.state);
  },

  runningJobs() {
    return this.state.jobs.filter((job) => job.status === "running");
  },

  startEvents() {
    if (typeof EventSource === "undefined") return;
    const source = new EventSource("/api/events");
    source.onopen = () => this.setState({ connected: true });
    source.onerror = () => this.setState({ connected: false });
    source.onmessage = (event) => {
      try {
        const payload = JSON.parse(event.data);
        this.setState({ jobs: payload.jobs || [], connected: true, lastEventAt: Date.now() });
      } catch {
        /* 忽略坏帧，SSE 会自动重连 */
      }
    };
  },
};
