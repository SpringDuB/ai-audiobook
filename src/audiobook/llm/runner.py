import logging
import random
import time

from pydantic import ValidationError

from .. import store
from .base import LLMDisconnected, LLMError, LLMRateLimit, LLMTimeout

logger = logging.getLogger(__name__)

REPAIR_HINT = (
    "\n\n上一次输出不是合法 JSON，错误信息：{error}\n"
    "只输出一个 JSON 对象：不要解释、不要 Markdown 代码块、不要多余文字。"
)


class LlmJsonError(RuntimeError):
    def __init__(self, message: str, attempts: int, last_error: str):
        super().__init__(message)
        self.attempts = attempts
        self.last_error = last_error


class LlmJsonRunner:
    """LLM 调用的唯一入口：并发门 + 严格 JSON 校验 + 失败重试一次 + 调用统计。"""

    def __init__(self, client, limiter, settings, max_attempts: int | None = None):
        self.client = client
        self.limiter = limiter
        self.settings = settings
        limit = settings.llm_max_attempts if max_attempts is None else max_attempts
        self.max_attempts = max(1, int(limit))

    def run(
        self,
        *,
        system: str,
        user: str,
        model_cls,
        pass_name: str,
        book_id: str,
        chapter_index: int | None = None,
        scene_id: str | None = None,
    ):
        last_error = ""
        for attempt in range(1, self.max_attempts + 1):
            prompt = user if attempt == 1 else user + REPAIR_HINT.format(error=last_error)
            try:
                with self.limiter.slot():
                    reply = self.client.complete(
                        system, prompt, max_output_tokens=self.settings.llm_max_output_tokens
                    )
            except LLMError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                # 失败一律"抖动退避 + 重试"，绝不降并发：一次网络抖动会同时掐断
                # 多路连接，任何"连续失败就降档"的策略都会在几毫秒内把 10 路
                # 打到 1 路（用户明确要求：失败只重试，不要动并发）。
                if isinstance(exc, LLMRateLimit):
                    limit = self.limiter.record_rate_limit()
                    logger.warning(
                        "LLM 限流：pass=%s 第 %s/%s 次，退避后重试（并发保持 %s）",
                        pass_name,
                        attempt,
                        self.max_attempts,
                        limit,
                    )
                    self._backoff(attempt)
                elif isinstance(exc, (LLMDisconnected, LLMTimeout)):
                    limit = self.limiter.record_transport_error()
                    logger.warning(
                        "LLM %s：pass=%s 第 %s/%s 次，退避后重试（并发保持 %s）",
                        type(exc).__name__,
                        pass_name,
                        attempt,
                        self.max_attempts,
                        limit,
                    )
                    self._backoff(attempt)
                self._log(
                    book_id,
                    pass_name,
                    chapter_index,
                    scene_id,
                    attempt,
                    False,
                    None,
                    last_error,
                    duration_ms=getattr(exc, "duration_ms", None),
                )
                continue
            except TimeoutError as exc:  # 等并发槽超时（内置 TimeoutError，不是 LLMTimeout）
                limit = self.limiter.record_rate_limit()
                last_error = f"等待 LLM 并发槽超时: {exc}"
                logger.warning("LLM 等槽超时：pass=%s（并发保持 %s）", pass_name, limit)
                self._backoff(attempt)
                self._log(book_id, pass_name, chapter_index, scene_id, attempt, False, None, last_error)
                continue
            self.limiter.record_success()
            try:
                model = model_cls.model_validate_json(reply.text)
            except ValidationError as exc:
                last_error = f"JSON 校验失败: {exc}"[:800]
                self._log(book_id, pass_name, chapter_index, scene_id, attempt, False, reply, last_error)
                continue
            self._log(book_id, pass_name, chapter_index, scene_id, attempt, True, reply, None)
            return model
        raise LlmJsonError(
            f"{pass_name} 趟分析连续 {self.max_attempts} 次失败", self.max_attempts, last_error
        )

    def _backoff(self, attempt: int) -> None:
        """重试前带抖动地等一会儿；已经是最后一次尝试就不再等。"""
        if attempt >= self.max_attempts:
            return
        delay = min(8.0, 1.0 * attempt) * (0.5 + random.random())
        time.sleep(delay)

    def _log(
        self, book_id, pass_name, chapter_index, scene_id, attempt, ok, reply, error, duration_ms=None
    ) -> None:
        if not book_id:
            return
        store.append_jsonl(
            store.llm_log_path(self.settings, book_id),
            {
                "ts": int(time.time() * 1000),
                "book_id": book_id,
                "pass": pass_name,
                "chapter": chapter_index,
                "scene": scene_id,
                "attempt": attempt,
                "ok": ok,
                "model": getattr(reply, "model", None),
                "duration_ms": getattr(reply, "duration_ms", 0) or duration_ms or 0,
                "input_tokens": getattr(reply, "input_tokens", None),
                "output_tokens": getattr(reply, "output_tokens", None),
                "reasoning_tokens": getattr(reply, "reasoning_tokens", None),
                "error": error,
            },
        )
