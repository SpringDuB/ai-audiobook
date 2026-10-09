"""Qwen3-TTS 逐帧解码加速：替换 code_predictor 里那 15 次 HF `generate()`。

上游每生成 1 帧音频（80ms）要做：

    1 × Talker 前向（28 层）
   15 × code_predictor.generate()   ← 每层残差 codebook 一次

而 code_predictor.generate() 是 **HF 的完整 generate()**，每个 step 都要重走一遍：

    deepcopy(generation_config) → 建 warper / logits processor / DynamicCache
    → 维护 attention_mask → 判停（CPU/GPU 同步）→ 拼返回值对象

一帧 16 次前向 ≈ 800~1000 次内核启动，实测（见 data/tmp-debug/qwen3_speed_bench.py）
每帧要 130ms 上下，而纯权重带宽只需要 ~25ms —— 瓶颈是 CPU 侧的启动/调度开销，
不是 GPU 算力，所以 nvidia-smi 上利用率一直在 0/100 之间跳。

本模块提供四条语义等价的快路径（都用贪婪解码对拍验证过）：

  loop  手写采样循环 + DynamicCache：去掉 HF generate 的全部簿记与判停同步。
  pad   定长前缀、无 KV cache：整段前缀（17 个位置）一次前向，位置/因果掩码和
        增量解码逐位等价，但形状固定 —— 这是能上 torch.compile / CUDA Graph 的前提。
  graph = pad + **手工 CUDA Graph**（实测 2.4x）：把每个 codebook 步的上百个内核
        捕获成一张图，之后每步只 replay 一次；采样留在图外（避开图内 RNG 的坑）。
        注意：本机 torch.compile/inductor 走不通（Windows 无 triton；补了
        triton-windows 后 reduce-overhead 仍会 32 位 long 溢出），所以用原生图。
  padc  = pad + torch.compile(mode="reduce-overhead")，仅作对照保留。

安装方式：`install_fast_predictor(model, mode=...)`，只挂一个实例方法，不改上游包。
"""

from __future__ import annotations

import contextlib
import logging
import threading
import types

import torch

logger = logging.getLogger(__name__)

# 正在捕获 CUDA Graph 的线程数。捕获窗口里别的线程做 cudaFree（empty_cache）或往
# 同一个 stream 提交 kernel，都会让捕获方直接报
# "CUDA error: operation not permitted when stream is capturing"。
# 服务端是 4 路并发：一次多角色试听就会同时在跑推理 + 懒捕获，必须把这件事变成
# 全局可见状态（state 的空闲归还据此让路）并让捕获走自己的 stream。
_CAPTURE_STATE_LOCK = threading.Lock()
_CAPTURING = 0


def capture_active() -> bool:
    """有没有线程正在捕获 CUDA Graph（空闲归还显存前必须先问一句）。"""
    with _CAPTURE_STATE_LOCK:
        return _CAPTURING > 0


@contextlib.contextmanager
def _capture_window():
    global _CAPTURING
    with _CAPTURE_STATE_LOCK:
        _CAPTURING += 1
    try:
        yield
    finally:
        with _CAPTURE_STATE_LOCK:
            _CAPTURING -= 1


class _InferenceGate:
    """读多写一：普通推理共享，捕获 CUDA Graph 时独占。

    捕获期间必须没有别的线程在提交 GPU 工作 —— 否则 torch 的 graph pool 记账会乱，
    实测报 "Offset increment outside graph capture encountered unexpectedly"
    （多路并发试听时 4 个请求挂了 2 个）。捕获只在某个形状第一次出现时发生，
    独占窗口很短，平时推理照旧并行。
    """

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._readers = 0
        self._writer = False

    @contextlib.contextmanager
    def shared(self):
        with self._cond:
            while self._writer:
                self._cond.wait()
            self._readers += 1
        try:
            yield
        finally:
            with self._cond:
                self._readers -= 1
                if self._readers == 0:
                    self._cond.notify_all()

    @contextlib.contextmanager
    def exclusive(self):
        with self._cond:
            while self._writer or self._readers:
                self._cond.wait()
            self._writer = True
        try:
            yield
        finally:
            with self._cond:
                self._writer = False
                self._cond.notify_all()


class _PredictorOutput:
    """调用方只用到 `.sequences`（Talker.forward 里的一行），不必拼完整输出对象。"""

    __slots__ = ("sequences",)

    def __init__(self, sequences: torch.Tensor):
        self.sequences = sequences


def _sample(logits, do_sample, top_k, top_p, temperature):
    """与 HF 的 Temperature/TopK/TopP warper + softmax + multinomial 等价。

    top_p == 1.0 时 HF 根本不建 TopP warper，这里同样跳过排序，省一次 sort。
    """
    if not do_sample:
        return logits.argmax(dim=-1, keepdim=True)
    scores = logits / max(float(temperature), 1e-5)
    vocab = scores.shape[-1]
    top_p = 1.0 if top_p is None else float(top_p)
    if top_p < 1.0:
        probs = torch.softmax(scores, dim=-1)
        ordered, positions = torch.sort(probs, dim=-1, descending=True)
        cumulative = ordered.cumsum(dim=-1)
        drop = cumulative - ordered > top_p
        ordered = ordered.masked_fill(drop, 0.0)
        picked = torch.multinomial(ordered, num_samples=1)
        return positions.gather(-1, picked)
    k = vocab if not top_k else min(int(top_k), vocab)
    if k < vocab:
        values, positions = torch.topk(scores, k, dim=-1)
        probs = torch.softmax(values, dim=-1)
        return positions.gather(-1, torch.multinomial(probs, num_samples=1))
    return torch.multinomial(torch.softmax(scores, dim=-1), num_samples=1)


def _pick_steps(predictor, max_new_tokens):
    if max_new_tokens is None:
        return int(predictor.config.num_code_groups) - 1
    return int(max_new_tokens)


def _loop_generate(
    self,
    inputs_embeds=None,
    max_new_tokens=None,
    do_sample=True,
    top_p=1.0,
    top_k=50,
    temperature=0.9,
    output_hidden_states=False,
    return_dict_in_generate=False,
    **kwargs,
):
    """快路径 A：手写循环 + DynamicCache。

    与上游的唯一差别：不传 attention_mask（无 padding 时 SDPA 的 is_causal 路径更快，
    而 HF 传的全 1 mask 每层都会走一次 `torch.all(mask == 1)` 判等，反而更慢）。
    """
    steps = _pick_steps(self, max_new_tokens)
    outputs = self(inputs_embeds=inputs_embeds, use_cache=True, return_dict=True)
    past = outputs.past_key_values
    generation_step = outputs.generation_steps
    logits = outputs.logits[:, -1, :]
    tokens = []
    for index in range(steps):
        token = _sample(logits, do_sample, top_k, top_p, temperature)
        tokens.append(token)
        if index + 1 >= steps:
            break
        outputs = self(
            input_ids=token,
            past_key_values=past,
            use_cache=True,
            generation_steps=generation_step,
            return_dict=True,
        )
        past = outputs.past_key_values
        generation_step = outputs.generation_steps
        logits = outputs.logits[:, -1, :]
    return _PredictorOutput(torch.cat(tokens, dim=-1))


def _pad_generate(
    self,
    inputs_embeds=None,
    max_new_tokens=None,
    do_sample=True,
    top_p=1.0,
    top_k=50,
    temperature=0.9,
    output_hidden_states=False,
    return_dict_in_generate=False,
    **kwargs,
):
    """快路径 B：定长前缀 + 无 cache（形状恒定，可编译/可图捕获）。

    前缀布局（总长 2 + steps = 17）：
        位置 0        ：past_hidden（Talker 上一步隐状态，2048 维）
        位置 1        ：上一层 codebook 的 embedding（Talker 第一个 token）
        位置 k+1 (k≥1)：第 k 个 codebook 的 embedding

    第 k 个 arm 取位置 k+1 的隐状态、过 lm_head[k] 采样 —— 与增量解码逐位等价：
    位置 k+1 只能看见 0..k+1，后面的填充位在因果掩码之外，不影响它。
    """
    steps = _pick_steps(self, max_new_tokens)
    embeddings = self.model.get_input_embeddings()
    heads = self.lm_head
    project = self.small_to_mtp_projection
    batch, hidden_size = inputs_embeds.shape[0], inputs_embeds.shape[-1]

    buffer = inputs_embeds.new_zeros(batch, 2 + steps, hidden_size)
    buffer[:, :2] = inputs_embeds

    tokens = []
    for index in range(steps):
        hidden = self.model(
            inputs_embeds=project(buffer), use_cache=False, return_dict=True
        ).last_hidden_state
        logits = heads[index](hidden[:, index + 1, :])
        token = _sample(logits, do_sample, top_k, top_p, temperature)
        tokens.append(token)
        if index + 1 >= steps:
            break
        buffer = buffer.clone()
        buffer[:, index + 2, :] = embeddings[index](token).squeeze(1)
    return _PredictorOutput(torch.cat(tokens, dim=-1))


class _GraphPredictor:
    """把 code_predictor 的 "projection + 5 层 trunk" 捕获成 CUDA Graph，按 batch 缓存。

    捕获的是一整段定长前缀（2 + steps = 17 个位置）的前向，形状恒定：
        buffer[B, 17, 2048] → projection → trunk → hidden[B, 17, 1024]
    每步只需要 replay 一次图，head / 采样 / 写回 embedding 留在图外的 eager 里。
    """

    def __init__(self, predictor, batch: int, steps: int):
        self.predictor = predictor
        self.batch = int(batch)
        self.steps = int(steps)
        self.project = predictor.small_to_mtp_projection
        self.trunk = predictor.model
        self.heads = predictor.lm_head
        self.embeddings = predictor.model.get_input_embeddings()
        parameter = next(predictor.parameters())
        self.device = parameter.device
        self.dtype = parameter.dtype
        talker_hidden = self.project.in_features
        hidden_size = predictor.config.hidden_size
        length = 2 + self.steps
        self.buffer = torch.zeros(self.batch, length, talker_hidden, device=self.device, dtype=self.dtype)
        self.hidden = torch.empty(self.batch, length, hidden_size, device=self.device, dtype=self.dtype)
        self.capture()

    def _body(self) -> None:
        with torch.inference_mode():
            output = self.trunk(
                inputs_embeds=self.project(self.buffer), use_cache=False, return_dict=False
            )
            self.hidden.copy_(output[0])

    def capture(self) -> None:
        """在**自己的 stream** 上捕获，绝不占用默认 stream。

        服务端最多 4 路并发：别的请求此刻正在默认 stream 上跑推理，如果这里直接
        在默认 stream 上捕获，对方提交的 kernel 会被卷进捕获窗口，直接报
        "operation not permitted when stream is capturing"（多角色试听踩过）。
        所以：先在私有 stream 上预热，再在私有 stream 上捕获，并用
        capture_error_mode="thread_local" 允许别的线程照常跑自己的活。
        """
        stream = torch.cuda.Stream(device=self.device)
        current = torch.cuda.current_stream(self.device)
        stream.wait_stream(current)
        with torch.cuda.stream(stream):
            for _ in range(2):  # 预热：让 cublas/cudnn 句柄、工作区先分配好
                self._body()
        current.wait_stream(stream)
        self.graph = torch.cuda.CUDAGraph()
        with _capture_window(), torch.cuda.graph(
            self.graph, stream=stream, capture_error_mode="thread_local"
        ):
            self._body()
        current.wait_stream(stream)
        self.graph.replay()
        current.synchronize()

    def run(self, inputs_embeds, do_sample, top_k, top_p, temperature):
        buffer, hidden = self.buffer, self.hidden
        buffer[:, :2].copy_(inputs_embeds[:, :2])
        tokens = []
        for index in range(self.steps):
            self.graph.replay()
            logits = self.heads[index](hidden[:, index + 1, :])
            token = _sample(logits, do_sample, top_k, top_p, temperature)
            tokens.append(token)
            if index + 1 >= self.steps:
                break
            buffer[:, index + 2].copy_(self.embeddings[index](token).squeeze(1))
        return torch.cat(tokens, dim=-1)


class _GraphGenerators:
    """按 (batch, steps) 懒捕获图，并把 generate(...) 调用接到图上。

    图里用的是**静态缓冲区**，所以一张图同一时刻只能服务一个请求：每种形状一张图、
    一把请求锁，后来的请求排队（实测多路并发对这张卡本来就是负收益）。捕获只在
    某个形状第一次出现时发生，并且走 `_InferenceGate.exclusive()`，捕获窗口里
    保证没有别的线程在提交 GPU 工作。
    """

    MAX_BATCH = 16
    MAX_SHAPES = 16

    def __init__(self, predictor):
        self.predictor = predictor
        self._lock = threading.Lock()  # 保护下面两个 dict
        self._capture_lock = threading.Lock()  # 捕获串行化（不同形状之间）
        self._gate = _InferenceGate()
        # value=False 表示这张形状捕获失败过，直接走 eager
        self._runners: dict[tuple[int, int], "_GraphPredictor | bool"] = {}
        self._shape_locks: dict[tuple[int, int], threading.Lock] = {}

    def clear(self) -> None:
        """丢掉捕获过的图（卸载/换模型时用）。"""
        with self._lock:
            self._runners.clear()
            self._shape_locks.clear()

    def _shape_lock(self, key: tuple[int, int]) -> threading.Lock:
        with self._lock:
            return self._shape_locks.setdefault(key, threading.Lock())

    def _store(self, key: tuple[int, int], runner) -> None:
        with self._lock:
            if key not in self._runners and len(self._runners) >= self.MAX_SHAPES:
                self._runners.clear()  # 形状太多：整批丢掉重建，别无限攒显存
            self._runners[key] = runner

    def _eager(self, inputs_embeds, steps, do_sample, top_k, top_p, temperature):
        return _pad_generate(
            self.predictor,
            inputs_embeds=inputs_embeds,
            max_new_tokens=steps,
            do_sample=do_sample,
            top_p=top_p,
            top_k=top_k,
            temperature=temperature,
        )

    def __call__(
        self,
        inputs_embeds=None,
        max_new_tokens=None,
        do_sample=True,
        top_p=1.0,
        top_k=50,
        temperature=0.9,
        output_hidden_states=False,
        return_dict_in_generate=False,
        **kwargs,
    ):
        steps = _pick_steps(self.predictor, max_new_tokens)
        batch = int(inputs_embeds.shape[0])
        if batch > self.MAX_BATCH:  # 超大包不值得为它单独捕获，退回 eager pad
            return self._eager(inputs_embeds, steps, do_sample, top_k, top_p, temperature)
        key = (batch, steps)
        with self._shape_lock(key):
            runner = self._runners.get(key)
            if runner is None:
                # 捕获期间独占：别的线程既不能捕获，也不能提交普通推理
                with self._capture_lock, self._gate.exclusive():
                    logger.info("捕获 code_predictor CUDA Graph：batch=%d steps=%d", batch, steps)
                    try:
                        runner = _GraphPredictor(self.predictor, batch, steps)
                    except Exception:  # noqa: BLE001 - 捕获失败不能拖垮这一次合成
                        logger.exception(
                            "捕获 CUDA Graph 失败（batch=%d steps=%d），这张形状退回 eager 快路径",
                            batch,
                            steps,
                        )
                        runner = False
                self._store(key, runner)
            if runner is False:  # 捕获失败过：直接走 eager，别再试
                return self._eager(inputs_embeds, steps, do_sample, top_k, top_p, temperature)
            with self._gate.shared():
                return _PredictorOutput(
                    runner.run(inputs_embeds, do_sample, top_k, top_p, temperature)
                )


_MODES = {
    "loop": _loop_generate,
    "pad": _pad_generate,
    "padc": _pad_generate,  # 先用 pad 的语义跑通，compile 在 install 里包一层
}


def install_fast_predictor(model, mode: str = "loop") -> str:
    """把 code_predictor.generate 换成快路径；返回一行说明文本。

    model：Qwen3TTSForConditionalGeneration（不是 Qwen3TTSModel 包装器）
    """
    predictor = model.talker.code_predictor
    if mode == "graph":
        generator = _GraphGenerators(predictor)
        predictor.generate = generator
        predictor._aiab_graph_generators = generator
        return f"code_predictor.generate → graph（手工 CUDA Graph，按 batch 懒捕获）"
    if mode not in _MODES:
        raise ValueError(f"未知 mode={mode}，可选：{sorted(_MODES) + ['graph']}")
    function = _MODES[mode]
    if mode == "padc":
        function = _compile_pad(predictor, _pad_generate)
    predictor.generate = types.MethodType(function, predictor)
    return f"code_predictor.generate → {mode}（predictor={type(predictor).__name__}）"


def uninstall_fast_predictor(model) -> None:
    predictor = model.talker.code_predictor
    generators = predictor.__dict__.pop("_aiab_graph_generators", None)
    if generators is not None:
        generators.clear()
    if "generate" in predictor.__dict__:
        del predictor.__dict__["generate"]


def _compile_pad(predictor, function):
    """pad + torch.compile(reduce-overhead)：形状固定，交给 inductor 融合/图捕获。"""
    try:
        compiled = torch.compile(function, mode="reduce-overhead", dynamic=False)
        # 先空跑一次触发编译，失败就老实退回未编译版本
        probe = torch.zeros(1, 2, predictor.small_to_mtp_projection.in_features)
        device = next(predictor.parameters()).device
        compiled(
            predictor,
            inputs_embeds=probe.to(device=device, dtype=next(predictor.parameters()).dtype),
            max_new_tokens=1,
            do_sample=False,
        )
        logger.info("code_predictor 已启用 torch.compile(reduce-overhead)")
        return compiled
    except Exception:  # noqa: BLE001 - 编译失败不该拖垮服务，退回 eager 快路径
        logger.exception("torch.compile 失败，回退 pad 快路径")
        return function
