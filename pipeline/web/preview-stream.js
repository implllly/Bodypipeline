"use strict";
window.ClientPreviewStream = (() => {
  const maxFrameBytes = 4 * 1024 * 1024;
  const maxHeaderBytes = 16384;

  async function* frames(response, signal) {
    if (!response.ok || !response.body) throw Error("相机视频流暂不可用");
    const type = response.headers.get("content-type") || "";
    const match = /boundary=(?:"([^"]+)"|([^;\s]+))/i.exec(type);
    const boundary = match?.[1] || match?.[2];
    if (!/^multipart\/x-mixed-replace\s*;/i.test(type) || !boundary || boundary.length > 70) {
      throw Error("相机视频流格式无效");
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = new Uint8Array(0);
    async function readMore() {
      if (signal.aborted) throw new DOMException("Aborted", "AbortError");
      const {value, done} = await reader.read();
      if (done) throw Error("相机视频流已断开");
      const merged = new Uint8Array(buffer.length + value.length);
      merged.set(buffer);
      merged.set(value, buffer.length);
      buffer = merged;
    }
    try {
      while (!signal.aborted) {
        let end = -1;
        while (end < 0) {
          for (let i = 0; i + 3 < buffer.length; i++) {
            if (buffer[i] === 13 && buffer[i + 1] === 10 && buffer[i + 2] === 13 && buffer[i + 3] === 10) {
              end = i;
              break;
            }
          }
          if (end < 0) {
            if (buffer.length > maxHeaderBytes) throw Error("相机视频流头部过大");
            await readMore();
          }
        }
        if (end > maxHeaderBytes) throw Error("相机视频流头部过大");
        const lines = decoder.decode(buffer.subarray(0, end)).replace(/^\r\n/, "").split("\r\n");
        if (lines.shift() !== "--" + boundary) throw Error("相机视频流边界无效");
        const headers = new Headers();
        for (const line of lines) {
          const colon = line.indexOf(":");
          if (colon < 1) throw Error("相机视频流头部无效");
          headers.append(line.slice(0, colon).trim(), line.slice(colon + 1).trim());
        }
        const size = headers.get("content-length") || "";
        const length = Number(size);
        if (!/^\d+$/.test(size) || length < 1 || length > maxFrameBytes || headers.get("content-type")?.toLowerCase() !== "image/jpeg") {
          throw Error("相机视频帧无效");
        }
        buffer = buffer.subarray(end + 4);
        while (buffer.length < length) await readMore();
        yield new Blob([buffer.slice(0, length)], {type: "image/jpeg"});
        buffer = buffer.subarray(length);
      }
    } finally {
      await reader.cancel().catch(() => {});
      reader.releaseLock();
    }
  }

  function retry(signal) {
    return new Promise(resolve => {
      function done() {
        clearTimeout(timer);
        signal.removeEventListener("abort", done);
        resolve();
      }
      const timer = setTimeout(done, 1000);
      signal.addEventListener("abort", done, {once: true});
      if (signal.aborted) done();
    });
  }

  async function run({url, signal, display, failed}) {
    while (!signal.aborted) {
      const connection = new AbortController();
      const abort = () => connection.abort();
      signal.addEventListener("abort", abort, {once: true});
      let pending = null, drawing = null, drawError = null;
      let watchdog;
      function alive() {
        clearTimeout(watchdog);
        watchdog = setTimeout(abort, 8000);
      }
      async function draw() {
        while (pending && !connection.signal.aborted) {
          const blob = pending;
          pending = null;
          await display(blob, connection.signal);
        }
      }
      try {
        if (signal.aborted) break;
        alive();
        const response = await fetch(url, {signal: connection.signal});
        for await (const blob of frames(response, connection.signal)) {
          if (connection.signal.aborted) break;
          if (drawError) throw drawError;
          alive();
          // A slow display keeps only the newest frame, never a growing queue.
          pending = blob;
          if (!drawing) {
            drawing = draw().catch(error => { drawError = error; abort(); }).finally(() => { drawing = null; });
          }
        }
      } catch {
        if (!signal.aborted) failed();
      } finally {
        clearTimeout(watchdog);
        connection.abort();
        signal.removeEventListener("abort", abort);
        pending = null;
        await drawing;
      }
      if (!signal.aborted) await retry(signal);
    }
  }
  return {frames, run};
})();
