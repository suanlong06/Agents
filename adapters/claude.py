"""Adapter Claude Code native (PR-2): tuyến "Khó" và chuỗi fallback Opus.

- Chạy ``claude -p --output-format json --model <model> --effort <effort>``, prompt
  qua stdin. Không dùng ``--fallback-model`` của CLI: fallback do router làm, để
  ledger ghi đúng model phục vụ và lý do fallback (R5/R11).
- Model THỰC SỰ phục vụ lấy từ trường ``modelUsage`` trong JSON kết quả (metadata
  có cấu trúc của CLI, ``model_id_source = cli_metadata``). Text ``result`` chỉ là
  dữ liệu: tên model nào xuất hiện trong đó cũng không được dùng.
- Một lời gọi có thể dùng nhiều model (CLI gọi model phụ cho việc nền): mỗi model
  là một bản ghi ledger riêng và đều vào writer set.
- ``input_tokens`` = input + cache creation + cache read: đếm dư thay vì đếm thiếu,
  vì ledger chỉ có một loại giá input (ngân sách vẫn fail-closed).
- Trường token thiếu hoặc sai kiểu KHÔNG BAO GIỜ được coi là 0 với ``estimated =
  false`` (đếm thiếu im lặng, R5): khi đó token là ước lượng (không thấp hơn phần đã
  biết) và ``estimated = true``.
"""

from __future__ import annotations

from .base import CallRequest, CallResult, ModelUsage, ServedModel, ServiceUnavailable, estimate_tokens
from .transport import CliAdapter, _int_or_none, parse_json_object

DEFAULT_COMMAND = ("claude", "-p", "--output-format", "json", "--model", "{model}", "--effort", "{effort}")
# Mọi trường phải CÓ MẶT trong mỗi mục ``modelUsage`` thì token mới là số đo thật.
MODEL_USAGE_INPUT_KEYS = ("inputTokens", "cacheCreationInputTokens", "cacheReadInputTokens")
MODEL_USAGE_OUTPUT_KEY = "outputTokens"


class ClaudeAdapter(CliAdapter):
    name = "claude"
    vendors = frozenset({"anthropic"})

    def __init__(self, *, command=DEFAULT_COMMAND, **kw):
        super().__init__(command=command, **kw)

    def _invoke(self, request: CallRequest, *, timeout_s: float, probe: bool) -> CallResult:
        res = self.execute(request, timeout_s=timeout_s)
        meta = parse_json_object(res.stdout)
        if meta is None:
            # Không có metadata có cấu trúc: model KHÔNG RÕ, không đoán từ stdout.
            usage = ModelUsage(ServedModel.unknown(), estimate_tokens(request.prompt),
                               estimate_tokens(res.stdout), True)
            return CallResult(self.name, request, (usage,), res.stdout)
        output = meta.get("result") if isinstance(meta.get("result"), str) else ""
        if meta.get("is_error"):
            raise ServiceUnavailable(
                f"claude/{request.model}: is_error ({meta.get('subtype')}): {output[:300]}"
            )
        session = meta.get("session_id") if isinstance(meta.get("session_id"), str) else None
        usages = self._model_usages(meta, session or f"call:{request.call_id}", request, output)
        if not usages:
            usages = (self._unknown_usage(request, meta, output, session),)
        return CallResult(self.name, request, usages, output, (), session)

    def _model_usages(self, meta: dict, session: str, request: CallRequest,
                      output: str) -> tuple[ModelUsage, ...]:
        model_usage = meta.get("modelUsage")
        if not isinstance(model_usage, dict):
            return ()
        out = []
        for raw, u in model_usage.items():
            if not isinstance(u, dict):
                continue
            served = self.aliases.served(raw, "cli_metadata")
            # Kiểm có mặt, không mặc định 0: thiếu trường nào thì token là ước lượng.
            parts = [_int_or_none(u[k]) if k in u else None for k in MODEL_USAGE_INPUT_KEYS]
            out_tokens = _int_or_none(u[MODEL_USAGE_OUTPUT_KEY]) if MODEL_USAGE_OUTPUT_KEY in u else None
            if out_tokens is None or any(p is None for p in parts):
                known_in = sum(p for p in parts if p is not None)
                out.append(ModelUsage(
                    served, max(known_in, estimate_tokens(request.prompt)),
                    out_tokens if out_tokens is not None else estimate_tokens(output), True,
                ))
                continue
            d_in, d_out = self.to_delta((session, raw), sum(parts), out_tokens)
            out.append(ModelUsage(served, d_in, d_out, False))
        return tuple(out)

    def _unknown_usage(self, request: CallRequest, meta: dict, output: str, session: str | None) -> ModelUsage:
        usage = meta.get("usage") if isinstance(meta.get("usage"), dict) else {}
        parts = [_int_or_none(usage.get(k, 0)) for k in
                 ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")]
        out_tokens = _int_or_none(usage.get("output_tokens"))
        if out_tokens is None or any(p is None for p in parts) or "input_tokens" not in usage:
            return ModelUsage(ServedModel.unknown(), estimate_tokens(request.prompt), estimate_tokens(output), True)
        d_in, d_out = self.to_delta((session or f"call:{request.call_id}", None), sum(parts), out_tokens)
        return ModelUsage(ServedModel.unknown(), d_in, d_out, False)
