export class RequestTooLargeError extends Error {
  constructor(maxBytes: number) {
    super(`Request body is too large (maximum ${maxBytes} bytes).`);
    this.name = "RequestTooLargeError";
  }
}

/**
 * Parse JSON without first buffering an unbounded request body. A declared
 * Content-Length over the limit is refused before any byte is read; a body
 * that grows past the limit while streaming is cut off at that point.
 */
export async function readBoundedJson(
  request: Request,
  maxBytes: number,
): Promise<unknown> {
  const contentLength = request.headers.get("content-length");
  if (contentLength && /^\d+$/.test(contentLength.trim())) {
    if (Number(contentLength) > maxBytes) throw new RequestTooLargeError(maxBytes);
  }

  if (!request.body) throw new SyntaxError("Request has no body.");

  const reader = request.body.getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      total += value.byteLength;
      if (total > maxBytes) {
        // Best effort: a failed cancel must not turn the 413 into a 400.
        await reader.cancel().catch(() => undefined);
        throw new RequestTooLargeError(maxBytes);
      }
      chunks.push(value);
    }
  } finally {
    reader.releaseLock();
  }

  const bytes = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes));
}
