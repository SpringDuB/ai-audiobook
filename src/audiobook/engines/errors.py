class TtsError(RuntimeError):
    """TTS 服务调用失败（网络、协议、引擎内部错误）。"""


class TtsBusy(TtsError):
    """服务端忙/限流：可退避重试。"""


class TtsOom(TtsError):
    """显存不足：必须降档 + 熔断。"""


class TtsUnavailable(TtsError):
    """模型未加载或服务不可达。"""


class TtsBadRef(TtsError):
    """参考音频失效（refId 未知/过期）：重新上传后可重试。"""


class TtsVoiceMissing(TtsBadRef):
    """本地缺少该音色的参考音频文件。"""


class TtsBadRequest(TtsError):
    """请求本身有问题（参数越界、文本为空）：重试无意义。"""


_CODE_TO_ERROR = {
    "busy": TtsBusy,
    "oom": TtsOom,
    "not_loaded": TtsUnavailable,
    "bad_ref": TtsBadRef,
    "bad_request": TtsBadRequest,
}


def classify_status(status_code: int, code: str | None) -> type[TtsError]:
    if code in _CODE_TO_ERROR:
        return _CODE_TO_ERROR[code]
    if status_code == 429:
        return TtsBusy
    if status_code in (502, 503, 504):
        return TtsUnavailable
    if status_code == 404:
        return TtsBadRef
    if status_code == 400:
        return TtsBadRequest
    return TtsError
