export function duration(seconds) {
  const total = Math.max(0, Math.round(Number(seconds) || 0));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  if (h > 0) return `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
  return `${m}:${String(s).padStart(2, "0")}`;
}

export function clock(ms) {
  if (!ms) return "—";
  const date = new Date(Number(ms));
  if (Number.isNaN(date.getTime())) return "—";
  return `${date.getMonth() + 1}/${date.getDate()} ${String(date.getHours()).padStart(2, "0")}:${String(date.getMinutes()).padStart(2, "0")}`;
}

export function shortId(id, size = 8) {
  return id ? String(id).slice(0, size) : "—";
}

const STATE_LABELS = {
  empty: "空",
  split: "待分析",
  analyzing: "分析中",
  analyzed: "已分析",
  synthesizing: "合成中",
  synthesized: "已合成",
  ready: "已完成",
  rendered: "已出成品",
};

export function stateLabel(state) {
  return STATE_LABELS[state] || state || "未知";
}

const KIND_LABELS = {
  chapter_split: "分章",
  characters: "角色分析",
  scenes: "场景切分",
  lines: "逐句标注",
  casting: "自动选角",
  synthesize: "章节合成",
  synthesize_line: "单句重合成",
  post: "章节渲染",
  book_export: "整本导出",
};

export function kindLabel(kind) {
  return KIND_LABELS[kind] || kind;
}

const JOB_STATE_LABELS = {
  queued: "排队",
  running: "运行中",
  done: "完成",
  failed: "失败",
  canceled: "已取消",
};

export function jobStateLabel(status) {
  return JOB_STATE_LABELS[status] || status;
}

const ISSUE_KIND_LABELS = {
  pass_a_failed: "角色分析失败",
  pass_a_chapter_skipped: "章节跳过",
  pass_b_failed: "场景切分失败",
  scene_hint_not_found: "场景线索未命中",
  pass_c_failed: "逐句标注失败",
  line_index_missing: "句索引缺失",
  unknown_speaker: "未知说话人",
  voice_library_empty: "音色库为空",
  casting_voice_reused: "音色复用",
  casting_no_match: "无匹配音色",
  tts_line_failed: "合成失败",
  tts_ref_missing: "参考音频缺失",
  tts_endpoint_down: "TTS 不可用",
  audio_missing: "音频缺失",
  render_duration_mismatch: "字幕时长不符",
  book_export_gap: "整本缺口",
};

export function issueKindLabel(kind) {
  return ISSUE_KIND_LABELS[kind] || kind;
}

export function percent(done, total) {
  if (!total) return 0;
  return Math.max(0, Math.min(100, Math.round((Number(done) / Number(total)) * 100)));
}
