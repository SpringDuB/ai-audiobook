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
    if (typeof EventSource === "undefined" || this.source) return;
    // v=2：首帧 snapshot + 之后只推变化的那几条（任务多的时候帧从 675KB 降到几百字节）
    const source = new EventSource("/api/events?v=2");
    this.source = source;
    source.onopen = () => this.setState({ connected: true });
    source.onerror = () => this.setState({ connected: false });
    source.onmessage = (event) => {
      let payload;
      try {
        payload = JSON.parse(event.data);
      } catch {
        return; // 坏帧忽略，SSE 会自动重连
      }
      if (Array.isArray(payload.jobs)) {
        // 首帧（或老后端的全量帧）：整份替换
        this.setState({ jobs: payload.jobs, connected: true, lastEventAt: Date.now() });
        return;
      }
      if (Array.isArray(payload.changed) || Array.isArray(payload.removed)) {
        // 增量帧：按 id 合并，别整列表重建
        const jobs = new Map(this.state.jobs.map((job) => [job.id, job]));
        for (const job of payload.changed || []) jobs.set(job.id, job);
        for (const id of payload.removed || []) jobs.delete(id);
        this.setState({
          jobs: [...jobs.values()].sort((a, b) => a.id - b.id),
          connected: true,
          lastEventAt: Date.now(),
        });
      }
    };
  },
};
