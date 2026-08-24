"""流式行装配器: 跨 chunk 安全的 UTF-8 解码与 JSONL 增量解析。

设计要点:
  - 手动维护字节缓冲,完整尝试解码; 遇到非法序列时仅丢弃该段
    非法字节并上报一次错误,其前后的合法数据全部保留;
  - 不完整的跨 chunk 多字节尾部留在缓冲中等待后续数据;
  - 行缓冲以字节计量,单行超限时立即上报并丢弃该行剩余部分
    (不保留原文),直到下一个换行恢复解析;
  - 每个完整行立刻回调,进程运行中即产出事件。
"""

from __future__ import annotations

from collections.abc import Callable

from adapters.cli.events import MAX_LINE_BYTES


class StreamAssembler:
    """把字节块流转成完整文本行的增量装配器。

    回调语义:
      - ``on_line(text)``: 每个完整行调用一次(不含换行符);
      - ``on_oversized(total_bytes)``: 某行超过 MAX_LINE_BYTES 时调用,
        只携带字节数; 该行后续内容被丢弃且不再产生 on_line;
      - ``on_decode_error(count)``: 每丢弃一段非法 UTF-8 字节时调用。
    """

    __slots__ = (
        "_line_buf",
        "_line_bytes",
        "_oversize_reported",
        "_pending",
        "_skipping",
        "on_decode_error",
        "on_line",
        "on_oversized",
    )

    def __init__(
        self,
        on_line: Callable[[str], None],
        on_oversized: Callable[[int], None],
        on_decode_error: Callable[[int], None],
    ) -> None:
        self._pending = bytearray()
        self._line_buf: list[str] = []
        self._line_bytes = 0
        self._oversize_reported = False
        self._skipping = False
        self.on_line = on_line
        self.on_oversized = on_oversized
        self.on_decode_error = on_decode_error

    def feed(self, data: bytes) -> None:
        """喂入一个原始字节块。"""

        self._pending.extend(data)
        self._drain_pending(final=False)

    def flush(self) -> None:
        """EOF 时调用: 处理无换行结尾的最后一行与残缺尾序列。"""

        self._drain_pending(final=True)
        self._finish_line()

    # ------------------------------------------------------------------

    def _drain_pending(self, *, final: bool) -> None:
        view = bytes(self._pending)
        offset = 0
        drop_run = 0

        def flush_drop() -> None:
            nonlocal drop_run
            if drop_run:
                self.on_decode_error(drop_run)
                drop_run = 0

        while offset < len(view):
            chunk = view[offset:]
            try:
                text = chunk.decode("utf-8")
            except UnicodeDecodeError as exc:
                if exc.start > 0:
                    # 非法序列之前的合法前缀先正常输出。
                    self._emit_text(chunk[: exc.start].decode("utf-8"))
                    offset += exc.start
                    chunk = view[offset:]
                    flush_drop()
                    if not chunk:
                        break
                bad_len = max(exc.end - exc.start, 1)
                incomplete_tail = (
                    not final
                    and exc.reason == "unexpected end of data"
                    and offset + exc.end >= len(view)
                )
                if incomplete_tail:
                    # 尾部是多字节序列前缀: 留待下一个 chunk 续上。
                    break
                # 相邻的连续非法字节合并为一次错误上报。
                drop_run += bad_len
                offset += bad_len
                continue
            flush_drop()
            self._emit_text(text)
            offset += len(chunk)
        flush_drop()
        self._pending = bytearray(view[offset:]) if offset < len(view) else bytearray()

    def _emit_text(self, text: str) -> None:
        for char in text:
            if char == "\n":
                self._finish_line()
                continue
            if self._skipping:
                continue
            piece_len = len(char.encode("utf-8"))
            if self._line_bytes + piece_len > MAX_LINE_BYTES:
                # 单行超限: 只报告长度,丢弃该行剩余内容。
                if not self._oversize_reported:
                    self.on_oversized(self._line_bytes + piece_len)
                    self._oversize_reported = True
                self._skipping = True
                self._line_buf.clear()
                continue
            self._line_buf.append(char)
            self._line_bytes += piece_len

    def _finish_line(self) -> None:
        buffered = "".join(self._line_buf)
        was_oversized = self._skipping
        self._line_buf = []
        self._line_bytes = 0
        self._oversize_reported = False
        self._skipping = False
        if was_oversized or not buffered.strip():
            return
        self.on_line(buffered)


__all__ = ["StreamAssembler"]
